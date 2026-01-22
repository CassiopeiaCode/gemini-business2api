import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from core.account import load_accounts_from_source
from core.base_task_service import BaseTask, BaseTaskService, TaskStatus
from core.config import config
from core.duckmail_client import DuckMailClient
from core.chatgpt_mail_client import ChatGPTMailClient
from core.gemini_automation import GeminiAutomation
from core.gemini_automation_uc import GeminiAutomationUC
from core.microsoft_mail_client import MicrosoftMailClient

logger = logging.getLogger("gemini.login")


@dataclass
class LoginTask(BaseTask):
    """登录任务数据类"""
    account_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """转换为字典"""
        base_dict = super().to_dict()
        base_dict["account_ids"] = self.account_ids
        return base_dict


class LoginService(BaseTaskService[LoginTask]):
    """登录服务类"""

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
        register_service: Optional[Any] = None,
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
            log_prefix="REFRESH",
        )
        self._is_polling = False
        self.register_service = register_service

    async def start_login(self, account_ids: List[str]) -> LoginTask:
        """启动登录任务"""
        async with self._lock:
            if self._current_task_id:
                current = self._tasks.get(self._current_task_id)
                if current and current.status == TaskStatus.RUNNING:
                    raise ValueError("login task already running")

            task = LoginTask(id=str(uuid.uuid4()), account_ids=account_ids)
            self._tasks[task.id] = task
            self._current_task_id = task.id
            self._append_log(task, "info", f"login task created ({len(account_ids)} accounts)")
            asyncio.create_task(self._run_login_async(task))
            return task

    async def _run_login_async(self, task: LoginTask) -> None:
        """异步执行登录任务"""
        task.status = TaskStatus.RUNNING
        loop = asyncio.get_running_loop()
        self._append_log(task, "info", "login task started")

        for account_id in task.account_ids:
            try:
                result = await loop.run_in_executor(self._executor, self._refresh_one, account_id, task)
            except Exception as exc:
                result = {"success": False, "email": account_id, "error": str(exc)}
            task.progress += 1
            task.results.append(result)

            if result.get("success"):
                task.success_count += 1
                self._append_log(task, "info", f"refresh success: {account_id}")
            else:
                task.fail_count += 1
                self._append_log(task, "error", f"refresh failed: {account_id} - {result.get('error')}")

        task.status = TaskStatus.SUCCESS if task.fail_count == 0 else TaskStatus.FAILED
        task.finished_at = time.time()
        self._current_task_id = None
        self._append_log(task, "info", f"login task finished ({task.success_count}/{len(task.account_ids)})")

    def _refresh_one(self, account_id: str, task: LoginTask) -> dict:
        """刷新单个账户"""
        accounts = load_accounts_from_source()
        account = next((acc for acc in accounts if acc.get("id") == account_id), None)
        if not account:
            return {"success": False, "email": account_id, "error": "account not found"}

        if account.get("disabled"):
            return {"success": False, "email": account_id, "error": "account disabled"}

        # 获取邮件提供商
        mail_provider = (account.get("mail_provider") or "").lower()
        if not mail_provider:
            if account.get("mail_client_id") or account.get("mail_refresh_token"):
                mail_provider = "microsoft"
            else:
                mail_provider = "duckmail"

        # 获取邮件配置
        mail_password = account.get("mail_password") or account.get("email_password")
        mail_client_id = account.get("mail_client_id")
        mail_refresh_token = account.get("mail_refresh_token")
        mail_tenant = account.get("mail_tenant") or "consumers"

        log_cb = lambda level, message: self._append_log(task, level, f"[{account_id}] {message}")

        # 创建邮件客户端
        if mail_provider == "microsoft":
            if not mail_client_id or not mail_refresh_token:
                return {"success": False, "email": account_id, "error": "microsoft oauth missing"}
            mail_address = account.get("mail_address") or account_id
            client = MicrosoftMailClient(
                client_id=mail_client_id,
                refresh_token=mail_refresh_token,
                tenant=mail_tenant,
                proxy=config.basic.proxy,
                log_callback=log_cb,
            )
            client.set_credentials(mail_address)
        elif mail_provider == "chatgpt_mail" or mail_provider == "chatgpt":
            # ChatGPT Mail: 不需要密码，只需要邮箱地址
            client = ChatGPTMailClient(
                base_url=config.basic.chatgpt_mail_base_url,
                proxy=config.basic.proxy,
                verify_ssl=True,
                log_callback=log_cb,
            )
            client.set_credentials(account_id)
        elif mail_provider == "duckmail":
            if not mail_password:
                return {"success": False, "email": account_id, "error": "mail password missing"}
            # DuckMail: account_id 就是邮箱地址
            client = DuckMailClient(
                base_url=config.basic.duckmail_base_url,
                proxy=config.basic.proxy,
                verify_ssl=config.basic.duckmail_verify_ssl,
                api_key=config.basic.duckmail_api_key,
                log_callback=log_cb,
            )
            client.set_credentials(account_id, mail_password)
        else:
            return {"success": False, "email": account_id, "error": f"unsupported mail provider: {mail_provider}"}

        # 浏览器代理：优先使用 browser_proxy；为空则回退到 proxy（兼容旧配置）
        browser_proxy = (config.basic.browser_proxy or "").strip() or (config.basic.proxy or "").strip()

        # 根据配置选择浏览器引擎
        browser_engine = (config.basic.browser_engine or "dp").lower()
        if browser_engine == "dp":
            # DrissionPage 引擎：支持有头和无头模式
            automation = GeminiAutomation(
                user_agent=self.user_agent,
                proxy=browser_proxy,
                headless=config.basic.browser_headless,
                log_callback=log_cb,
            )
        else:
            # undetected-chromedriver 引擎：支持有头和无头
            automation = GeminiAutomationUC(
                user_agent=self.user_agent,
                proxy=browser_proxy,
                headless=config.basic.browser_headless,
                log_callback=log_cb,
            )
        try:
            result = automation.login_and_extract(account_id, client)
        except Exception as exc:
            return {"success": False, "email": account_id, "error": str(exc)}
        if not result.get("success"):
            return {"success": False, "email": account_id, "error": result.get("error", "automation failed")}

        # 更新账户配置
        config_data = result["config"]
        config_data["mail_provider"] = mail_provider
        
        # 根据邮件提供商保存不同的凭证
        if mail_provider == "microsoft":
            config_data["mail_address"] = account.get("mail_address") or account_id
            config_data["mail_client_id"] = mail_client_id
            config_data["mail_refresh_token"] = mail_refresh_token
            config_data["mail_tenant"] = mail_tenant
        elif mail_provider in ("chatgpt_mail", "chatgpt"):
            # ChatGPT Mail 不需要密码
            config_data["mail_password"] = ""
        else:
            # DuckMail 需要密码
            config_data["mail_password"] = mail_password
            
        config_data["disabled"] = account.get("disabled", False)

        for acc in accounts:
            if acc.get("id") == account_id:
                acc.update(config_data)
                break

        self._apply_accounts_update(accounts)
        return {"success": True, "email": account_id, "config": config_data}


    def _get_expiring_accounts(self) -> List[str]:
        accounts = load_accounts_from_source()
        expiring = []
        beijing_tz = timezone(timedelta(hours=8))
        now = datetime.now(beijing_tz)

        for account in accounts:
            if account.get("disabled"):
                continue
            mail_provider = (account.get("mail_provider") or "").lower()
            if not mail_provider:
                if account.get("mail_client_id") or account.get("mail_refresh_token"):
                    mail_provider = "microsoft"
                else:
                    mail_provider = "duckmail"

            mail_password = account.get("mail_password") or account.get("email_password")
            if mail_provider == "microsoft":
                if not account.get("mail_client_id") or not account.get("mail_refresh_token"):
                    continue
            elif mail_provider in ("chatgpt_mail", "chatgpt"):
                # ChatGPT Mail 不需要密码验证，可以直接刷新
                pass
            else:
                # DuckMail 需要密码
                if not mail_password:
                    continue
            expires_at = account.get("expires_at")
            if not expires_at:
                continue

            try:
                expire_time = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
                expire_time = expire_time.replace(tzinfo=beijing_tz)
                remaining = (expire_time - now).total_seconds() / 3600
            except Exception:
                continue

            if remaining <= config.basic.refresh_window_hours:
                expiring.append(account.get("id"))

        return expiring

    async def _auto_heal_account_pool(self) -> None:
        """自愈逻辑：监控账户池健康度，自动触发注册任务"""
        if not self.register_service:
            logger.info("[HEAL] register service not available, auto-heal disabled")
            return

        logger.info("[HEAL] account pool health monitor started")
        
        # 使用与 login_refresh_polling_seconds 相同的间隔
        while self._is_polling:
            try:
                # 获取当前轮询间隔
                polling_seconds = int(getattr(config.retry, "login_refresh_polling_seconds", 1800) or 0)
                if polling_seconds <= 0:
                    logger.info("[HEAL] polling disabled, stopping health monitor")
                    break

                await asyncio.sleep(polling_seconds)

                if os.environ.get("ACCOUNTS_CONFIG"):
                    continue

                # 计算不可用账户占比
                candidates = [
                    acc for acc in self.multi_account_mgr.accounts.values()
                    if (not acc.config.disabled) and (not acc.config.is_expired())
                ]
                total = len(candidates)
                
                if total == 0:
                    logger.warning("[HEAL] no available accounts in pool")
                    continue

                unavailable = sum(1 for acc in candidates if not acc.should_retry())
                ratio = unavailable / total

                logger.info(
                    "[HEAL] account pool status: %s/%s unavailable (%.1f%%)",
                    unavailable,
                    total,
                    ratio * 100.0,
                )

                # 触发自愈：占比 > 60% 且没有注册任务在运行
                if ratio > 0.60:
                    current_task = self.register_service.get_current_task()
                    is_running = bool(current_task and current_task.status == TaskStatus.RUNNING)
                    
                    if is_running:
                        logger.info("[HEAL] register task already running, skipping auto-heal")
                    else:
                        logger.warning(
                            "[HEAL] triggering auto-heal: unavailable ratio too high (%.0f%%), registering 10 new accounts",
                            ratio * 100.0,
                        )
                        try:
                            await self.register_service.start_register(count=10)
                            logger.info("[HEAL] auto-heal register task started successfully")
                        except Exception as exc:
                            logger.error("[HEAL] failed to start register task: %s", exc, exc_info=True)

            except asyncio.CancelledError:
                logger.info("[HEAL] health monitor stopped")
                break
            except Exception as exc:
                logger.error("[HEAL] health monitor error: %s", exc, exc_info=True)
                await asyncio.sleep(60)  # 出错后等待60秒再继续

    async def check_and_refresh(self) -> None:
        """检查并刷新即将过期的账户，删除已过期账户"""
        if os.environ.get("ACCOUNTS_CONFIG"):
            logger.info("[LOGIN] ACCOUNTS_CONFIG set, skipping refresh")
            return

        try:
            accounts = load_accounts_from_source()
            beijing_tz = timezone(timedelta(hours=8))
            now = datetime.now(beijing_tz)
            
            accounts_to_refresh = []
            accounts_to_delete = []
            
            for account in accounts:
                if account.get("disabled"):
                    continue
                    
                account_id = account.get("id")
                expires_at = account.get("expires_at")
                
                if not expires_at:
                    continue

                # 检查邮件凭证是否完整
                mail_provider = (account.get("mail_provider") or "").lower()
                if not mail_provider:
                    if account.get("mail_client_id") or account.get("mail_refresh_token"):
                        mail_provider = "microsoft"
                    else:
                        mail_provider = "duckmail"

                has_credentials = False
                if mail_provider == "microsoft":
                    has_credentials = bool(account.get("mail_client_id") and account.get("mail_refresh_token"))
                elif mail_provider in ("chatgpt_mail", "chatgpt"):
                    has_credentials = True  # ChatGPT Mail 不需要密码
                else:  # duckmail
                    has_credentials = bool(account.get("mail_password") or account.get("email_password"))

                if not has_credentials:
                    continue

                # 解析过期时间
                try:
                    expire_time = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
                    expire_time = expire_time.replace(tzinfo=beijing_tz)
                    remaining_hours = (expire_time - now).total_seconds() / 3600
                except Exception as parse_exc:
                    logger.error(
                        "[LOGIN] failed to parse expires_at for account %s: %s (value: %s)",
                        account_id,
                        parse_exc,
                        expires_at,
                    )
                    continue

                # 判断处理策略
                if remaining_hours < -1:
                    # 已过期超过1小时：删除
                    logger.warning(
                        "[LOGIN] account %s expired %.1f hours ago, marking for deletion",
                        account_id,
                        abs(remaining_hours),
                    )
                    accounts_to_delete.append(account_id)
                elif remaining_hours <= config.basic.refresh_window_hours:
                    # 即将过期或刚过期不到1小时：刷新
                    if remaining_hours < 0:
                        logger.info(
                            "[LOGIN] account %s expired %.1f hours ago (within grace period), marking for refresh",
                            account_id,
                            abs(remaining_hours),
                        )
                    else:
                        logger.info(
                            "[LOGIN] account %s will expire in %.1f hours, marking for refresh",
                            account_id,
                            remaining_hours,
                        )
                    accounts_to_refresh.append(account_id)

            # 删除已过期账户
            if accounts_to_delete:
                logger.info("[LOGIN] deleting %s expired accounts: %s", len(accounts_to_delete), accounts_to_delete)
                try:
                    updated_accounts = [acc for acc in accounts if acc.get("id") not in accounts_to_delete]
                    self._apply_accounts_update(updated_accounts)
                    logger.info("[LOGIN] successfully deleted %s expired accounts", len(accounts_to_delete))
                except Exception as delete_exc:
                    logger.error("[LOGIN] failed to delete expired accounts: %s", delete_exc, exc_info=True)

            # 刷新即将过期的账户
            if accounts_to_refresh:
                logger.info("[LOGIN] refreshing %s expiring accounts: %s", len(accounts_to_refresh), accounts_to_refresh)
                try:
                    await self.start_login(accounts_to_refresh)
                except ValueError as exc:
                    logger.warning("[LOGIN] %s", exc)
                except Exception as refresh_exc:
                    logger.error("[LOGIN] failed to start refresh task: %s", refresh_exc, exc_info=True)
            else:
                logger.debug("[LOGIN] no accounts need refresh")

        except Exception as exc:
            logger.error("[LOGIN] check_and_refresh failed: %s", exc, exc_info=True)

    async def start_polling(self) -> None:
        polling_seconds = int(getattr(config.retry, "login_refresh_polling_seconds", 1800) or 0)
        if polling_seconds <= 0:
            logger.info("[LOGIN] refresh polling disabled (login_refresh_polling_seconds=0)")
            return

        if self._is_polling:
            logger.warning("[LOGIN] polling already running")
            return

        self._is_polling = True
        logger.info("[LOGIN] refresh polling started (interval: %s seconds)", polling_seconds)
        
        # 启动独立的自愈协程
        heal_task = None
        if self.register_service:
            heal_task = asyncio.create_task(self._auto_heal_account_pool())
            logger.info("[LOGIN] account pool health monitor started as separate task")
        
        try:
            while self._is_polling:
                await self.check_and_refresh()
                # 支持热更新：每轮读取一次最新配置
                polling_seconds = int(getattr(config.retry, "login_refresh_polling_seconds", polling_seconds) or 0)
                if polling_seconds <= 0:
                    logger.info("[LOGIN] refresh polling disabled during runtime (login_refresh_polling_seconds=0)")
                    break
                await asyncio.sleep(polling_seconds)
        except asyncio.CancelledError:
            logger.info("[LOGIN] polling stopped")
        except Exception as exc:
            logger.error("[LOGIN] polling error: %s", exc, exc_info=True)
        finally:
            self._is_polling = False
            # 取消自愈协程
            if heal_task and not heal_task.done():
                heal_task.cancel()
                try:
                    await heal_task
                except asyncio.CancelledError:
                    pass
                logger.info("[LOGIN] health monitor task cancelled")

    def stop_polling(self) -> None:
        self._is_polling = False
        logger.info("[LOGIN] stopping polling")
