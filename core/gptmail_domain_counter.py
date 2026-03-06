import json
import os
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


_lock = threading.Lock()


def _counter_file_path() -> str:
    # Docker/容器环境通常会挂载 /data；本地则使用仓库的 data 目录
    if os.path.exists("/data"):
        return "/data/gptmail_domail_counter.json"
    return "data/gptmail_domail_counter.json"


def _domain_from_email(email: str) -> Optional[str]:
    if not isinstance(email, str):
        return None
    email = email.strip()
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip().lower()
    return domain or None


def _safe_load() -> Dict[str, dict]:
    path = _counter_file_path()
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomic_write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def _safe_save(data: Dict[str, dict]) -> None:
    path = _counter_file_path()
    _atomic_write_json(path, data)


@dataclass(frozen=True)
class DomainStats:
    attempts: int
    success: int

    @property
    def success_rate(self) -> float:
        if self.attempts <= 0:
            return 0.0
        return float(self.success) / float(self.attempts)


def get_domain_stats(domain: str) -> DomainStats:
    domain = (domain or "").strip().lower()
    if not domain:
        return DomainStats(attempts=0, success=0)
    with _lock:
        data = _safe_load()
        row = data.get(domain) if isinstance(data, dict) else None
        if not isinstance(row, dict):
            return DomainStats(attempts=0, success=0)
        attempts = int(row.get("attempts") or 0)
        success = int(row.get("success") or 0)
        return DomainStats(attempts=max(0, attempts), success=max(0, success))


def increment_attempt(email: str) -> Optional[Tuple[str, DomainStats]]:
    domain = _domain_from_email(email)
    if not domain:
        return None
    with _lock:
        data = _safe_load()
        row = data.get(domain)
        if not isinstance(row, dict):
            row = {"attempts": 0, "success": 0}
            data[domain] = row
        row["attempts"] = int(row.get("attempts") or 0) + 1
        row["success"] = int(row.get("success") or 0)
        _safe_save(data)
        return domain, DomainStats(attempts=int(row["attempts"]), success=int(row["success"]))


def increment_success(email: str) -> Optional[Tuple[str, DomainStats]]:
    domain = _domain_from_email(email)
    if not domain:
        return None
    with _lock:
        data = _safe_load()
        row = data.get(domain)
        if not isinstance(row, dict):
            row = {"attempts": 0, "success": 0}
            data[domain] = row
        row["attempts"] = int(row.get("attempts") or 0)
        row["success"] = int(row.get("success") or 0) + 1
        _safe_save(data)
        return domain, DomainStats(attempts=int(row["attempts"]), success=int(row["success"]))


def should_refresh_once_for_domain(domain: str) -> bool:
    """
    当 domain 的历史成功率位于后 50%（排名靠后的一半）时返回 True。
    - domain 不在统计内 / 统计不足时返回 False
    - 至少需要 >= 2 个有 attempts 的域名才判断“后半”
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return False
    with _lock:
        data = _safe_load()

    stats: Dict[str, DomainStats] = {}
    for d, row in (data or {}).items():
        if not isinstance(d, str) or not isinstance(row, dict):
            continue
        attempts = int(row.get("attempts") or 0)
        success = int(row.get("success") or 0)
        if attempts <= 0:
            continue
        stats[d.strip().lower()] = DomainStats(attempts=max(0, attempts), success=max(0, success))

    if len(stats) < 2:
        return False
    if domain not in stats:
        return False

    ranked = sorted(
        stats.items(),
        key=lambda kv: (kv[1].success_rate, kv[1].attempts, kv[0]),
        reverse=True,
    )
    rank_index = next((i for i, (d, _) in enumerate(ranked) if d == domain), None)
    if rank_index is None:
        return False

    # 更偏“积极刷新”：奇数时中位也算在后半
    threshold = len(ranked) // 2
    return rank_index >= threshold

