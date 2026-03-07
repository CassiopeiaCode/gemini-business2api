import logging
from typing import Callable, Optional

from core.chatgpt_mail_client import ChatGPTMailClient
from core.config import config
from core.duckmail_client import DuckMailClient
from core.gemini_automation import GeminiAutomation
from core.gemini_automation_fp import GeminiAutomationFP
from core.gemini_automation_uc import GeminiAutomationUC
from core.gptmail_domain_counter import increment_attempt as gptmail_increment_attempt
from core.gptmail_domain_counter import increment_success as gptmail_increment_success
from core.proxy_helper import choose_random_httpx_proxy, choose_random_proxy

logger = logging.getLogger("gemini.register_runner")

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


def register_one_account(
    domain: Optional[str] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    log_callback: Optional[Callable[[str, str], None]] = None,
    mail_provider: Optional[str] = None,
    proxy: Optional[str] = None,
    browser_proxy: Optional[str] = None,
    duckmail_base_url: Optional[str] = None,
    duckmail_api_key: Optional[str] = None,
    duckmail_verify_ssl: Optional[bool] = None,
    chatgpt_mail_base_url: Optional[str] = None,
    chatgpt_mail_api_key: Optional[str] = None,
    browser_engine: Optional[str] = None,
    browser_headless: Optional[bool] = None,
    fp_chrome_path: Optional[str] = None,
) -> dict:
    def log(level: str, message: str) -> None:
        if log_callback:
            log_callback(level, message)
            return
        if level == "warning":
            logger.warning(message)
        elif level == "error":
            logger.error(message)
        else:
            logger.info(message)

    http_proxy_value = (proxy if proxy is not None else config.basic.proxy or "").strip()
    mail_proxy = choose_random_httpx_proxy(http_proxy_value)
    provider = (mail_provider or config.basic.mail_provider or "duckmail").lower()

    if provider == "chatgpt":
        client = ChatGPTMailClient(
            base_url=chatgpt_mail_base_url or config.basic.chatgpt_mail_base_url,
            api_key=chatgpt_mail_api_key if chatgpt_mail_api_key is not None else getattr(config.basic, "chatgpt_mail_api_key", ""),
            proxy=mail_proxy,
            verify_ssl=True,
            log_callback=log,
        )
        if not client.register_account():
            return {"success": False, "error": "chatgpt mail register failed"}
        mail_provider_name = "chatgpt_mail"
    else:
        client = DuckMailClient(
            base_url=duckmail_base_url or config.basic.duckmail_base_url,
            proxy=mail_proxy,
            verify_ssl=config.basic.duckmail_verify_ssl if duckmail_verify_ssl is None else duckmail_verify_ssl,
            api_key=duckmail_api_key if duckmail_api_key is not None else config.basic.duckmail_api_key,
            log_callback=log,
        )
        if not client.register_account(domain=domain):
            return {"success": False, "error": "duckmail register failed"}
        mail_provider_name = "duckmail"

    browser_proxy_raw = (browser_proxy if browser_proxy is not None else config.basic.browser_proxy or "").strip() or http_proxy_value
    selected_browser_proxy = choose_random_proxy(browser_proxy_raw) or browser_proxy_raw
    engine = (browser_engine or config.basic.browser_engine or "dp").lower()
    headless = config.basic.browser_headless if browser_headless is None else browser_headless
    fp_path = fp_chrome_path if fp_chrome_path is not None else config.basic.fp_chrome_path

    if engine == "uc":
        automation = GeminiAutomationUC(
            user_agent=user_agent,
            proxy=selected_browser_proxy,
            headless=headless,
            log_callback=log,
        )
    elif engine in ("dp-fc", "fp"):
        automation = GeminiAutomationFP(
            user_agent=user_agent,
            proxy=selected_browser_proxy,
            headless=headless,
            log_callback=log,
            fp_chrome_path=fp_path,
        )
    else:
        automation = GeminiAutomation(
            user_agent=user_agent,
            proxy=selected_browser_proxy,
            headless=headless,
            log_callback=log,
        )

    try:
        if mail_provider_name == "chatgpt_mail" and client.email:
            gptmail_increment_attempt(client.email)
        result = automation.login_and_extract(client.email, client)
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    if not result.get("success"):
        return {"success": False, "error": result.get("error", "automation failed")}

    if mail_provider_name == "chatgpt_mail" and client.email:
        gptmail_increment_success(client.email)

    config_data = dict(result["config"])
    config_data["mail_provider"] = mail_provider_name
    config_data["mail_address"] = client.email
    config_data["mail_password"] = getattr(client, "password", "") or ""

    return {"success": True, "email": client.email, "config": config_data}
