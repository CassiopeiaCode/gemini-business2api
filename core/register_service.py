import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.account import load_accounts_from_source
from core.base_task_service import BaseTask, BaseTaskService, TaskStatus
from core.config import config
from core.register_runner import register_one_account

logger = logging.getLogger("gemini.register")


@dataclass
class RegisterTask(BaseTask):
    count: int = 0

    def to_dict(self) -> dict:
        base_dict = super().to_dict()
        base_dict["count"] = self.count
        return base_dict


class RegisterService(BaseTaskService[RegisterTask]):
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
            task.status = TaskStatus.RUNNING
            self._tasks[task.id] = task
            self._current_task_id = task.id
            self._append_log(task, "info", f"register task created (count={register_count})")
            asyncio.create_task(self._run_register_async(task, domain_value))
            return task

    async def _run_register_async(self, task: RegisterTask, domain: Optional[str]) -> None:
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
        log_cb = lambda level, message: self._append_log(task, level, message)
        result = register_one_account(domain=domain, user_agent=self.user_agent, log_callback=log_cb)
        if not result.get("success"):
            return result

        config_data = result["config"]
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

        return result
