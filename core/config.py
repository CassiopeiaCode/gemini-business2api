"""
Unified config management.
"""

import os
import secrets
from pathlib import Path
from typing import List, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from core import storage

load_dotenv()


def _parse_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "y", "on"):
            return True
        if lowered in ("0", "false", "no", "n", "off"):
            return False
    return default


class BasicConfig(BaseModel):
    api_key: str = Field(default="", description="API key")
    base_url: str = Field(default="", description="Service base URL")
    proxy: str = Field(default="", description="HTTP proxy")
    browser_proxy: str = Field(default="", description="Browser automation proxy")
    mail_provider: str = Field(default="duckmail", description="Mail provider")
    duckmail_base_url: str = Field(default="https://api.duckmail.sbs", description="DuckMail API URL")
    duckmail_api_key: str = Field(default="", description="DuckMail API key")
    duckmail_verify_ssl: bool = Field(default=True, description="DuckMail SSL verify")
    chatgpt_mail_base_url: str = Field(default="https://mail.chatgpt.org.uk", description="ChatGPT Mail API URL")
    chatgpt_mail_api_key: str = Field(default="", description="ChatGPT Mail API key")
    browser_engine: str = Field(default="dp", description="Browser engine")
    browser_headless: bool = Field(default=False, description="Headless browser mode")
    fp_chrome_path: str = Field(default="", description="FP engine browser binary path override (optional)")
    refresh_window_hours: int = Field(default=1, ge=0, le=24, description="Refresh window hours")
    register_default_count: int = Field(default=1, ge=1, le=30, description="Default register count")
    register_domain: str = Field(default="", description="Default register domain")
    sync_enabled: bool = Field(default=False, description="Enable host/slave sync")
    sync_secret: str = Field(default="", description="Host/slave sync secret")
    master_sync_url: str = Field(default="", description="Host sync endpoint URL")


class ImageGenerationConfig(BaseModel):
    enabled: bool = Field(default=True, description="Enable image generation")
    supported_models: List[str] = Field(default=["gemini-3-pro-preview"], description="Supported models")
    output_format: str = Field(default="base64", description="Image output format")


class VideoGenerationConfig(BaseModel):
    output_format: str = Field(default="html", description="Video output format")


class RetryConfig(BaseModel):
    max_new_session_tries: int = Field(default=5, ge=1, le=20, description="Max new session tries")
    max_request_retries: int = Field(default=3, ge=1, le=10, description="Max request retries")
    max_account_switch_tries: int = Field(default=5, ge=1, le=20, description="Max account switch tries")
    account_failure_threshold: int = Field(default=3, ge=1, le=1000, description="Account failure threshold")
    rate_limit_cooldown_seconds: int = Field(default=600, ge=1, le=3600, description="Rate limit cooldown seconds")
    session_cache_ttl_seconds: int = Field(default=3600, ge=0, le=86400, description="Session cache TTL seconds")
    auto_refresh_accounts_seconds: int = Field(default=60, ge=0, le=600, description="Auto refresh accounts seconds")
    login_refresh_polling_seconds: int = Field(default=1800, ge=0, le=86400, description="Login refresh polling seconds")
    auto_heal_cpu_load_threshold_percent: float = Field(
        default=30.0,
        ge=0.0,
        le=100.0,
        description="Auto-heal register CPU load ratio threshold percent (Linux only)",
    )


class PublicDisplayConfig(BaseModel):
    logo_url: str = Field(default="", description="Logo URL")
    chat_url: str = Field(default="", description="Chat URL")


class SessionConfig(BaseModel):
    expire_hours: int = Field(default=24, ge=1, le=168, description="Session expire hours")


class SecurityConfig(BaseModel):
    admin_key: str = Field(default="", description="Admin key")
    session_secret_key: str = Field(..., description="Session secret key")


class AppConfig(BaseModel):
    security: SecurityConfig
    basic: BasicConfig
    image_generation: ImageGenerationConfig
    video_generation: VideoGenerationConfig
    retry: RetryConfig
    public_display: PublicDisplayConfig
    session: SessionConfig


class ConfigManager:
    def __init__(self, yaml_path: str = None):
        if yaml_path is None:
            yaml_path = "/data/settings.yaml" if os.path.exists("/data") else "data/settings.yaml"
        self.yaml_path = Path(yaml_path)
        self._config: Optional[AppConfig] = None
        self.load()

    def load(self):
        yaml_data = self._load_yaml()
        security_config = SecurityConfig(
            admin_key=os.getenv("ADMIN_KEY", ""),
            session_secret_key=os.getenv("SESSION_SECRET_KEY", self._generate_secret()),
        )

        basic_data = yaml_data.get("basic", {})
        basic_config = BasicConfig(
            api_key=basic_data.get("api_key") or "",
            base_url=basic_data.get("base_url") or "",
            proxy=basic_data.get("proxy") or "",
            browser_proxy=basic_data.get("browser_proxy") or "",
            mail_provider=basic_data.get("mail_provider") or "duckmail",
            duckmail_base_url=basic_data.get("duckmail_base_url") or "https://api.duckmail.sbs",
            duckmail_api_key=str(basic_data.get("duckmail_api_key") or "").strip(),
            duckmail_verify_ssl=_parse_bool(basic_data.get("duckmail_verify_ssl"), True),
            chatgpt_mail_base_url=basic_data.get("chatgpt_mail_base_url") or "https://mail.chatgpt.org.uk",
            chatgpt_mail_api_key=str(basic_data.get("chatgpt_mail_api_key") or "").strip(),
            browser_engine=basic_data.get("browser_engine") or "dp",
            browser_headless=_parse_bool(basic_data.get("browser_headless"), False),
            fp_chrome_path=basic_data.get("fp_chrome_path") or "",
            refresh_window_hours=int(basic_data.get("refresh_window_hours", 1)),
            register_default_count=int(basic_data.get("register_default_count", 1)),
            register_domain=str(basic_data.get("register_domain") or "").strip(),
            sync_enabled=_parse_bool(basic_data.get("sync_enabled"), False),
            sync_secret=str(basic_data.get("sync_secret") or "").strip(),
            master_sync_url=str(basic_data.get("master_sync_url") or "").strip(),
        )

        retry_data = dict(yaml_data.get("retry", {}) or {})
        env_threshold = os.getenv("AUTO_HEAL_CPU_LOAD_THRESHOLD_PERCENT")
        if env_threshold is not None and str(env_threshold).strip() != "":
            try:
                retry_data["auto_heal_cpu_load_threshold_percent"] = float(str(env_threshold).strip())
            except Exception:
                print(
                    f"[WARN] invalid AUTO_HEAL_CPU_LOAD_THRESHOLD_PERCENT={env_threshold!r}; "
                    "falling back to settings/default"
                )

        self._config = AppConfig(
            security=security_config,
            basic=basic_config,
            image_generation=ImageGenerationConfig(**yaml_data.get("image_generation", {})),
            video_generation=VideoGenerationConfig(**yaml_data.get("video_generation", {})),
            retry=RetryConfig(**retry_data),
            public_display=PublicDisplayConfig(**yaml_data.get("public_display", {})),
            session=SessionConfig(**yaml_data.get("session", {})),
        )

    def _load_yaml(self) -> dict:
        if storage.is_database_enabled():
            try:
                data = storage.load_settings_sync()
                if isinstance(data, dict):
                    return data
            except Exception as exc:
                print(f"[WARN] Failed to load settings from database: {exc}; falling back to local file")
        if self.yaml_path.exists():
            try:
                with open(self.yaml_path, "r", encoding="utf-8") as handle:
                    return yaml.safe_load(handle) or {}
            except Exception as exc:
                print(f"[WARN] Failed to load settings file: {exc}; using defaults")
        return {}

    def _generate_secret(self) -> str:
        return secrets.token_urlsafe(32)

    def save_yaml(self, data: dict):
        if storage.is_database_enabled():
            try:
                saved = storage.save_settings_sync(data)
                if saved:
                    return
            except Exception as exc:
                print(f"[WARN] Failed to save settings to database: {exc}; falling back to local file")
        self.yaml_path.parent.mkdir(exist_ok=True)
        with open(self.yaml_path, "w", encoding="utf-8") as handle:
            yaml.dump(data, handle, allow_unicode=True, default_flow_style=False, sort_keys=False)

    def reload(self):
        self.load()

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def api_key(self) -> str:
        return self._config.basic.api_key

    @property
    def admin_key(self) -> str:
        return self._config.security.admin_key

    @property
    def session_secret_key(self) -> str:
        return self._config.security.session_secret_key

    @property
    def proxy(self) -> str:
        return self._config.basic.proxy

    @property
    def base_url(self) -> str:
        return self._config.basic.base_url

    @property
    def logo_url(self) -> str:
        return self._config.public_display.logo_url

    @property
    def chat_url(self) -> str:
        return self._config.public_display.chat_url

    @property
    def image_generation_enabled(self) -> bool:
        return self._config.image_generation.enabled

    @property
    def image_generation_models(self) -> List[str]:
        return self._config.image_generation.supported_models

    @property
    def image_output_format(self) -> str:
        return self._config.image_generation.output_format

    @property
    def video_output_format(self) -> str:
        return self._config.video_generation.output_format

    @property
    def session_expire_hours(self) -> int:
        return self._config.session.expire_hours

    @property
    def max_new_session_tries(self) -> int:
        return self._config.retry.max_new_session_tries

    @property
    def max_request_retries(self) -> int:
        return self._config.retry.max_request_retries

    @property
    def max_account_switch_tries(self) -> int:
        return self._config.retry.max_account_switch_tries

    @property
    def account_failure_threshold(self) -> int:
        return self._config.retry.account_failure_threshold

    @property
    def rate_limit_cooldown_seconds(self) -> int:
        return self._config.retry.rate_limit_cooldown_seconds

    @property
    def session_cache_ttl_seconds(self) -> int:
        return self._config.retry.session_cache_ttl_seconds

    @property
    def auto_refresh_accounts_seconds(self) -> int:
        return self._config.retry.auto_refresh_accounts_seconds

    @property
    def login_refresh_polling_seconds(self) -> int:
        return self._config.retry.login_refresh_polling_seconds


config_manager = ConfigManager()


def get_config() -> AppConfig:
    return config_manager.config


class _ConfigProxy:
    @property
    def basic(self):
        return config_manager.config.basic

    @property
    def security(self):
        return config_manager.config.security

    @property
    def image_generation(self):
        return config_manager.config.image_generation

    @property
    def video_generation(self):
        return config_manager.config.video_generation

    @property
    def retry(self):
        return config_manager.config.retry

    @property
    def public_display(self):
        return config_manager.config.public_display

    @property
    def session(self):
        return config_manager.config.session


config = _ConfigProxy()
