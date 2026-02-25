import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.account import load_accounts_from_source
from core.base_task_service import BaseTask, BaseTaskService, TaskStatus
from core.config import config
from core.duckmail_client import DuckMailClient
from core.chatgpt_mail_client import ChatGPTMailClient
from core.gemini_automation import GeminiAutomation
from core.gemini_automation_uc import GeminiAutomationUC
from core.gemini_automation_fp import GeminiAutomationFP

logger = logging.getLogger("gemini.register")


@dataclass
class RegisterTask(BaseTask):
    """注册任务数据类"""
    count: int = 0

    def to_dict(self) -> dict:
        """转换为字典"""
        base_dict = super().to_dict()
        base_dict["count"] = self.count
        return base_dict


class RegisterService(BaseTaskService[RegisterTask]):
    """注册服务类"""

    def __init__(
        self,
        multi_account_mgr,
        http_client,
        user_agent: str,
        account_failure_threshold: int,
        rate_limit_cooldown_seconds: int,
        session_cache_ttl_seconds: int,
        global_stats_provider: Callable[[], dict],
        set_multi_account_mgr: Optional[Callable[[Any], None]] = None,
    ) -> None:
        super().__init__(
            multi_account_mgr,
            http_client,
            user_agent,
            account_failure_threshold,
            rate_limit_cooldown_seconds,
            session_cache_ttl_seconds,
            global_stats_provider,
            set_multi_account_mgr,
            log_prefix="REGISTER",
        )

    async def start_register(self, count: Optional[int] = None, domain: Optional[str] = None) -> RegisterTask:
        """启动注册任务"""
        async with self._lock:
            if os.environ.get("ACCOUNTS_CONFIG"):
                raise ValueError("ACCOUNTS_CONFIG is set; register is disabled")
            if self._current_task_id:
                current = self._tasks.get(self._current_task_id)
                if current and current.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                    raise ValueError("register task already running")

            domain_value = (domain or "").strip()
            if not domain_value:
                domain_value = (config.basic.register_domain or "").strip() or None

            register_count = count or config.basic.register_default_count
            register_count = max(1, int(register_count))
            task = RegisterTask(id=str(uuid.uuid4()), count=register_count)
            # 在创建时就标记为 running，避免 create_task 调度前的并发窗口导致多开
            task.status = TaskStatus.RUNNING
            self._tasks[task.id] = task
            self._current_task_id = task.id
            self._append_log(task, "info", f"register task created (count={register_count})")
            asyncio.create_task(self._run_register_async(task, domain_value))
            return task

    async def _run_register_async(self, task: RegisterTask, domain: Optional[str]) -> None:
        """异步执行注册任务"""
        loop = asyncio.get_running_loop()
        self._append_log(task, "info", "register task started")

        try:
            for _ in range(task.count):
                try:
                    result = await loop.run_in_executor(self._executor, self._register_one, domain, task)
                except Exception as exc:
                    result = {"success": False, "error": str(exc)}
                task.progress += 1
                task.results.append(result)

                if result.get("success"):
                    task.success_count += 1
                    self._append_log(task, "info", f"register success: {result.get('email')}")
                else:
                    task.fail_count += 1
                    self._append_log(task, "error", f"register failed: {result.get('error')}")
        finally:
            task.status = TaskStatus.SUCCESS if task.fail_count == 0 else TaskStatus.FAILED
            task.finished_at = time.time()
            async with self._lock:
                if self._current_task_id == task.id:
                    self._current_task_id = None
            self._append_log(task, "info", f"register task finished ({task.success_count}/{task.count})")

    def _register_one(self, domain: Optional[str], task: RegisterTask) -> dict:
        """注册单个账户"""
        log_cb = lambda level, message: self._append_log(task, level, message)

        # HTTP 代理：支持逗号分隔多个代理，调用时随机选择一个；并支持 host:port:user:pass 格式
        from core.proxy_helper import choose_random_httpx_proxy
        mail_proxy = choose_random_httpx_proxy((config.basic.proxy or "").strip())
        
        # 根据配置选择邮箱提供商
        mail_provider = (config.basic.mail_provider or "duckmail").lower()
        
        if mail_provider == "chatgpt":
            # 使用 ChatGPT Mail 客户端
            client = ChatGPTMailClient(
                base_url=config.basic.chatgpt_mail_base_url,
                proxy=mail_proxy,
                verify_ssl=True,
                log_callback=log_cb,
            )
            if not client.register_account():
                return {"success": False, "error": "chatgpt mail register failed"}
            mail_provider_name = "chatgpt_mail"
        else:
            # 使用 DuckMail 客户端（默认）
            client = DuckMailClient(
                base_url=config.basic.duckmail_base_url,
                proxy=mail_proxy,
                verify_ssl=config.basic.duckmail_verify_ssl,
                api_key=config.basic.duckmail_api_key,
                log_callback=log_cb,
            )
            if not client.register_account(domain=domain):
                return {"success": False, "error": "duckmail register failed"}
            mail_provider_name = "duckmail"

        # 浏览器代理：支持逗号分隔多个代理，启动时随机选择一个
        from core.proxy_helper import choose_random_proxy
        browser_proxy_raw = (config.basic.browser_proxy or "").strip() or (config.basic.proxy or "").strip()
        browser_proxy = choose_random_proxy(browser_proxy_raw) or browser_proxy_raw

        # 根据配置选择浏览器引擎
        browser_engine = (config.basic.browser_engine or "dp").lower()
        if browser_engine == "uc":
            # undetected-chromedriver 引擎：支持有头和无头
            automation = GeminiAutomationUC(
                user_agent=self.user_agent,
                proxy=browser_proxy,
                headless=config.basic.browser_headless,
                log_callback=log_cb,
            )
        elif browser_engine == "dp-fc" or browser_engine == "fp":
            # DrissionPage + fingerprint-chromium 引擎
            automation = GeminiAutomationFP(
                user_agent=self.user_agent,
                proxy=browser_proxy,
                headless=config.basic.browser_headless,
                log_callback=log_cb,
                fp_chrome_path=config.basic.fp_chrome_path,
            )
        else:
            # DrissionPage 引擎（默认）：支持有头和无头模式
            automation = GeminiAutomation(
                user_agent=self.user_agent,
                proxy=browser_proxy,
                headless=config.basic.browser_headless,
                log_callback=log_cb,
            )

        try:
            result = automation.login_and_extract(client.email, client)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        if not result.get("success"):
            return {"success": False, "error": result.get("error", "automation failed")}

        config_data = result["config"]
        config_data["mail_provider"] = mail_provider_name
        config_data["mail_address"] = client.email
        # ChatGPT Mail 没有密码，DuckMail 才有
        if hasattr(client, 'password') and client.password:
            config_data["mail_password"] = client.password
        else:
            config_data["mail_password"] = ""

        accounts_data = load_accounts_from_source()
        updated = False
        for acc in accounts_data:
            if acc.get("id") == config_data["id"]:
                acc.update(config_data)
                updated = True
                break
        if not updated:
            accounts_data.append(config_data)

        self._apply_accounts_update(accounts_data)

        return {"success": True, "email": client.email, "config": config_data}
