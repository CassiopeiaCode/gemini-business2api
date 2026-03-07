import argparse
import logging
import os
import sys
from typing import Optional

import httpx

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

logger = logging.getLogger("slave_register")

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Register account once and push it to host")
    parser.add_argument("--master-sync-url", required=True)
    parser.add_argument("--sync-secret", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--domain", default="")
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--browser-proxy", default=None)
    parser.add_argument("--mail-provider", default=None)
    parser.add_argument("--duckmail-base-url", default=None)
    parser.add_argument("--duckmail-api-key", default=None)
    parser.add_argument("--duckmail-verify-ssl", action="store_true", default=None)
    parser.add_argument("--no-duckmail-verify-ssl", dest="duckmail_verify_ssl", action="store_false")
    parser.add_argument("--chatgpt-mail-base-url", default=None)
    parser.add_argument("--chatgpt-mail-api-key", default=None)
    parser.add_argument("--browser-engine", default=None)
    parser.add_argument("--browser-headless", action="store_true", default=None)
    parser.add_argument("--no-browser-headless", dest="browser_headless", action="store_false")
    parser.add_argument("--fp-chrome-path", default=None)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def push_account(master_sync_url: str, sync_secret: str, node_id: str, account: dict, timeout: float) -> dict:
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            master_sync_url,
            headers={"Authorization": f"Bearer {sync_secret}"},
            json={"source": node_id, "account": account},
        )
        response.raise_for_status()
        return response.json()


def _log_callback(level: str, message: str) -> None:
    line = f"[SLAVE-REGISTER] {message}"
    if level == "warning":
        logger.warning(line)
    elif level == "error":
        logger.error(line)
    else:
        logger.info(line)


def run_once(args: argparse.Namespace) -> int:
    from core.register_runner import register_one_account

    result = register_one_account(
        domain=(args.domain or "").strip() or None,
        user_agent=args.user_agent,
        log_callback=_log_callback,
        mail_provider=args.mail_provider,
        proxy=args.proxy,
        browser_proxy=args.browser_proxy,
        duckmail_base_url=args.duckmail_base_url,
        duckmail_api_key=args.duckmail_api_key,
        duckmail_verify_ssl=args.duckmail_verify_ssl,
        chatgpt_mail_base_url=args.chatgpt_mail_base_url,
        chatgpt_mail_api_key=args.chatgpt_mail_api_key,
        browser_engine=args.browser_engine,
        browser_headless=args.browser_headless,
        fp_chrome_path=args.fp_chrome_path,
    )
    if not result.get("success"):
        logger.error("[SLAVE-REGISTER] register failed: %s", result.get("error"))
        return 2

    try:
        sync_result = push_account(
            master_sync_url=args.master_sync_url,
            sync_secret=args.sync_secret,
            node_id=args.node_id,
            account=result["config"],
            timeout=args.timeout,
        )
    except Exception as exc:
        logger.error("[SLAVE-REGISTER] sync failed: %s", exc)
        return 3

    logger.info(
        "[SLAVE-REGISTER] synced account=%s email=%s action=%s",
        result["config"].get("id"),
        result.get("email"),
        sync_result.get("action"),
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.count < 1:
        logger.error("[SLAVE-REGISTER] --count must be >= 1")
        return 1

    final_code = 0
    for _ in range(args.count):
        code = run_once(args)
        if code != 0:
            final_code = code
            break
    return final_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        logger.exception("[SLAVE-REGISTER] unexpected error: %s", exc)
        raise SystemExit(4)
