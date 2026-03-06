import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.chatgpt_mail_client import ChatGPTMailClient  # noqa: E402
from core.mail_utils import extract_verification_code  # noqa: E402


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def _summarize_messages(messages: List[Dict[str, Any]], limit: int) -> str:
    if not messages:
        return "(empty)"
    lines: List[str] = []
    for msg in messages[: max(0, int(limit))]:
        subject = _safe_str(msg.get("subject")).strip()
        ts = msg.get("timestamp")
        from_addr = _safe_str(msg.get("from") or msg.get("from_address") or msg.get("sender")).strip()
        lines.append(f"- ts={ts} from={from_addr or 'N/A'} subject={subject[:120] if subject else 'N/A'}")
    return "\n".join(lines)


def _extract_code_from_messages(messages: List[Dict[str, Any]]) -> Optional[str]:
    for msg in messages:
        subject = _safe_str(msg.get("subject"))
        html = _safe_str(msg.get("html_content"))
        text = _safe_str(msg.get("content"))
        code = extract_verification_code(f"{subject} {html} {text}")
        if code:
            return code
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="ChatGPT Mail inbox polling test")
    parser.add_argument("--base-url", default="https://mail.chatgpt.org.uk", help="ChatGPT Mail base URL")
    parser.add_argument("--proxy", default="", help="HTTP proxy (e.g. http://host:port)")
    parser.add_argument("--verify-ssl", action="store_true", default=True, help="Verify SSL (default: true)")
    parser.add_argument("--no-verify-ssl", action="store_false", dest="verify_ssl", help="Disable SSL verify")
    parser.add_argument("--api-key", default="", help="GPTMail API key (e.g. gpt-test)")
    parser.add_argument("--gm-sid", default="", help="Inject gm_sid cookie from browser session (optional)")
    parser.add_argument("--inbox-token", default="", help="Inject X-Inbox-Token from browser session (optional)")
    parser.add_argument(
        "--referer-email",
        default="",
        help="Override Referer to https://mail.chatgpt.org.uk/<email> (optional, for stricter server checks)",
    )
    parser.add_argument("--interval", type=float, default=2.0, help="Polling interval seconds (default: 2)")
    parser.add_argument("--print-limit", type=int, default=5, help="Max messages to print per poll (default: 5)")
    parser.add_argument("--print-json", action="store_true", help="Also print raw JSON (truncated) per poll")
    parser.add_argument("--show-code", action="store_true", help="Try extract and print verification code")
    args = parser.parse_args()

    # 尽量避免中文乱码：Windows 下强制 utf-8 输出
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    client = ChatGPTMailClient(
        base_url=args.base_url,
        proxy=args.proxy,
        verify_ssl=bool(args.verify_ssl),
        api_key=(args.api_key or "").strip(),
        gm_sid=(args.gm_sid or "").strip() or None,
        inbox_token=(args.inbox_token or "").strip() or None,
        log_callback=lambda level, msg: print(f"[{level.upper()}] {msg}"),
    )
    if args.referer_email.strip():
        client.common_headers["Referer"] = f"{client.home_url}/{args.referer_email.strip()}"

    ok = client.register_account()
    if not ok or not client.email:
        print("[ERROR] register_account failed")
        print(
            "[HINT] 如果提示 Browser session required：从浏览器抓包复制 Cookie gm_sid 和请求头 X-Inbox-Token，"
            "然后用 --gm-sid / --inbox-token 传入再试。",
        )
        return 2

    print(f"[INFO] email: {client.email}")
    if client.inbox_token:
        print(f"[INFO] inbox_token: {client.inbox_token[:20]}... (expires_at={client.token_expires_at})")

    last_count: Optional[int] = None
    last_seen_code: Optional[str] = None
    start = datetime.now().isoformat(timespec="seconds")
    print(f"[INFO] polling started at {start} (interval={args.interval}s). Press Ctrl+C to stop.")

    try:
        while True:
            messages = client.fetch_messages()
            now = datetime.now().isoformat(timespec="seconds")
            count = len(messages) if isinstance(messages, list) else 0

            changed = (last_count is None) or (count != last_count)
            banner = "CHANGED" if changed else "same"
            print(f"\n[{now}] inbox={count} ({banner})")
            print(_summarize_messages(messages if isinstance(messages, list) else [], args.print_limit))

            if args.show_code:
                code = _extract_code_from_messages(messages if isinstance(messages, list) else [])
                if code and code != last_seen_code:
                    last_seen_code = code
                    print(f"[CODE] {code}")

            if args.print_json:
                try:
                    raw = json.dumps(messages, ensure_ascii=False)
                    print(f"[JSON] {raw[:2000]}{'...(truncated)' if len(raw) > 2000 else ''}")
                except Exception as exc:
                    print(f"[WARN] json dump failed: {exc}")

            last_count = count
            time.sleep(max(0.2, float(args.interval)))
    except KeyboardInterrupt:
        print("\n[INFO] stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
