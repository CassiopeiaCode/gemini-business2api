import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional
from datetime import datetime

import requests

from core.mail_utils import extract_verification_code
from core.gptmail_domain_counter import should_refresh_once_for_domain


class ChatGPTMailClient:
    """ChatGPT.org.uk 临时邮箱客户端"""

    def __init__(
        self,
        base_url: str = "https://mail.chatgpt.org.uk",
        proxy: str = "",
        verify_ssl: bool = True,
        api_key: str = "",
        gm_sid: Optional[str] = None,
        inbox_token: Optional[str] = None,
        log_callback=None,
    ) -> None:
        self.home_url = base_url.rstrip("/")
        self.base_url = f"{self.home_url}/api"
        self.verify_ssl = verify_ssl
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.log_callback = log_callback
        self.api_key = (api_key or "").strip()

        self.email: Optional[str] = None
        self._prefer_headless_fetch_messages = False
        # 近期版本 API 会在响应体里返回 auth.token / expires_at；用于后续请求鉴权
        self.inbox_token: Optional[str] = None
        self.token_expires_at: Optional[int] = None
        self.auth_email: Optional[str] = None
        self.session = requests.Session()  # 使用 Session 自动管理 Cookie

        # 允许外部注入浏览器会话 cookie/token（用于绕过 Browser session required）
        if isinstance(gm_sid, str) and gm_sid.strip():
            try:
                self._set_cookie_value("gm_sid", gm_sid.strip(), domain="mail.chatgpt.org.uk", path="/")
            except Exception:
                # 兜底：不带 domain
                self._set_cookie_value("gm_sid", gm_sid.strip())
        if isinstance(inbox_token, str) and inbox_token.strip():
            self.inbox_token = inbox_token.strip()
        
        # 通用请求头
        self.common_headers = {
            "sec-ch-ua": '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
            "Origin": self.home_url,
            "Referer": f"{self.home_url}/",
        }

        self._bridge_path = Path(__file__).resolve().parent / "gptmail_headless" / "bridge.mjs"
        self._headless_client_path = Path(__file__).resolve().parent / "gptmail_headless" / "client.mjs"

    def _get_cookie_value(self, name: str) -> str:
        exact_match = ""
        fallback_value = ""
        for cookie in self.session.cookies:
            if getattr(cookie, "name", None) != name:
                continue
            value = getattr(cookie, "value", "") or ""
            domain = (getattr(cookie, "domain", "") or "").lstrip(".").lower()
            if domain == "mail.chatgpt.org.uk":
                exact_match = value
            elif not fallback_value:
                fallback_value = value
        return exact_match or fallback_value

    def _set_cookie_value(self, name: str, value: str, domain: Optional[str] = None, path: str = "/") -> None:
        if not isinstance(value, str) or not value:
            return
        duplicates = []
        for cookie in self.session.cookies:
            if getattr(cookie, "name", None) == name:
                duplicates.append((cookie.domain, cookie.path, name))
        for cookie_domain, cookie_path, cookie_name in duplicates:
            try:
                self.session.cookies.clear(domain=cookie_domain, path=cookie_path, name=cookie_name)
            except Exception:
                pass
        if domain:
            self.session.cookies.set(name, value, domain=domain, path=path)
        else:
            self.session.cookies.set(name, value)

    def _cookie_dict(self) -> dict:
        cookie_names = []
        seen = set()
        for cookie in self.session.cookies:
            cookie_name = getattr(cookie, "name", None)
            if not cookie_name or cookie_name in seen:
                continue
            seen.add(cookie_name)
            cookie_names.append(cookie_name)
        return {
            cookie_name: cookie_value
            for cookie_name in cookie_names
            if (cookie_value := self._get_cookie_value(cookie_name))
        }

    def _is_headless_bridge_available(self) -> bool:
        return (
            self._bridge_path.exists()
            and self._headless_client_path.exists()
            and shutil.which("node") is not None
        )

    def _headless_state(self) -> dict:
        return {
            "email": self.email or "",
            "gm_sid": self._get_cookie_value("gm_sid"),
            "inbox_token": self.inbox_token or "",
            "token_expires_at": self.token_expires_at or 0,
            "auth_email": self.auth_email or "",
            "cookies": self._cookie_dict(),
            "proxy": (self.proxies or {}).get("https") or (self.proxies or {}).get("http") or "",
        }

    def _headless_proxy(self) -> str:
        return (self.proxies or {}).get("https") or (self.proxies or {}).get("http") or ""

    def _headless_env(self) -> dict:
        env = dict(os.environ)
        proxy = self._headless_proxy().strip()
        if proxy:
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy
            env.setdefault("NO_PROXY", "127.0.0.1,localhost")
            env.setdefault("no_proxy", "127.0.0.1,localhost")
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _sync_state_from_headless(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        email = (state.get("email") or "").strip()
        if email:
            self.email = email
        gm_sid = (state.get("gm_sid") or "").strip()
        if gm_sid:
            try:
                self._set_cookie_value("gm_sid", gm_sid, domain="mail.chatgpt.org.uk", path="/")
            except Exception:
                self._set_cookie_value("gm_sid", gm_sid)
        inbox_token = (state.get("inbox_token") or "").strip()
        if inbox_token:
            self.inbox_token = inbox_token
        token_expires_at = state.get("token_expires_at")
        if isinstance(token_expires_at, int) and token_expires_at > 0:
            self.token_expires_at = token_expires_at
        auth_email = (state.get("auth_email") or "").strip()
        if auth_email:
            self.auth_email = auth_email
        cookies = state.get("cookies")
        if isinstance(cookies, dict):
            for name, value in cookies.items():
                if isinstance(name, str) and isinstance(value, str):
                    if name == "gm_sid":
                        self._set_cookie_value(name, value, domain="mail.chatgpt.org.uk", path="/")
                    else:
                        self._set_cookie_value(name, value)

    def _run_headless_bridge(self, action: str, **payload) -> Optional[dict]:
        if not self._is_headless_bridge_available():
            return None
        request_payload = {
            "action": action,
            "origin": self.home_url,
            "state": self._headless_state(),
            "proxy": self._headless_proxy(),
            **payload,
        }
        try:
            result = subprocess.run(
                ["node", str(self._bridge_path)],
                input=json.dumps(request_payload, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=10,
                env=self._headless_env(),
            )
        except subprocess.TimeoutExpired as exc:
            stderr_output = exc.stderr or ""
            stdout_output = exc.stdout or ""
            if isinstance(stderr_output, bytes):
                stderr_output = stderr_output.decode("utf-8", errors="replace")
            if isinstance(stdout_output, bytes):
                stdout_output = stdout_output.decode("utf-8", errors="replace")
            if stderr_output:
                for line in stderr_output.splitlines():
                    line = line.strip()
                    if line:
                        self._log("info", f"[headless][timeout] {line}")
            if stdout_output:
                preview = stdout_output.strip()
                if preview:
                    self._log("info", f"[headless][timeout][stdout] {preview[:1000]}")
                    try:
                        response = json.loads(preview)
                    except Exception:
                        response = None
                    if isinstance(response, dict) and response.get("ok"):
                        self._log("warning", "headless bridge timed out after emitting a successful response; accepting partial stdout")
                        self._sync_state_from_headless(response.get("state") or {})
                        return response
            self._log("warning", f"headless bridge launch failed: {exc}")
            return None
        except Exception as exc:
            self._log("warning", f"headless bridge launch failed: {exc}")
            return None

        if result.stderr:
            for line in result.stderr.splitlines():
                line = line.strip()
                if line:
                    self._log("info", f"[headless] {line}")

        raw = (result.stdout or "").strip()
        if not raw:
            self._log("warning", "headless bridge returned empty output")
            return None
        try:
            response = json.loads(raw)
        except Exception as exc:
            self._log("warning", f"headless bridge invalid json: {exc}")
            return None

        if not response.get("ok"):
            self._log("warning", f"headless bridge failed: {response.get('error')}")
            return None

        self._sync_state_from_headless(response.get("state") or {})
        return response

    @staticmethod
    def _extract_emails_from_payload(payload: Optional[dict]) -> list:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        emails = data.get("emails")
        return emails if isinstance(emails, list) else []

    def _register_via_headless(self, reason: str) -> Optional[str]:
        self._log("warning", f"switching to headless register: {reason}")
        bridge_response = self._run_headless_bridge("register")
        if not bridge_response:
            self._log("error", f"headless register failed after: {reason}")
            return None
        result = bridge_response.get("result") or {}
        email = ((result.get("data") or {}).get("email") or "").strip()
        if not email:
            self._log("error", f"headless register returned no email after: {reason}")
            return None
        return email

    def set_credentials(self, email: str, password: Optional[str] = None) -> None:
        """设置邮箱凭证（此服务不需要密码）"""
        self.email = email

    def _update_auth_from_json(self, payload: dict) -> None:
        """从响应 JSON 中提取 auth 信息（若存在）"""
        try:
            auth = payload.get("auth") if isinstance(payload, dict) else None
            if not isinstance(auth, dict):
                return
            token = auth.get("token")
            if isinstance(token, str) and token.strip():
                self.inbox_token = token.strip()
            expires_at = auth.get("expires_at")
            if isinstance(expires_at, int):
                self.token_expires_at = expires_at
            email = auth.get("email")
            if isinstance(email, str) and email.strip():
                self.auth_email = email.strip()
        except Exception:
            return

    def _try_update_auth_from_response(self, res: requests.Response) -> None:
        """尽力从响应里更新 auth（不影响主流程）"""
        try:
            ct = (res.headers.get("content-type") or "").lower()
            if "application/json" not in ct:
                return
            if not res.content:
                return
            payload = res.json()
            if isinstance(payload, dict):
                self._update_auth_from_json(payload)
        except Exception:
            return

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """发送请求并打印详细日志"""
        headers = kwargs.pop("headers", None) or {}
        # 合并通用请求头
        headers = {**self.common_headers, **headers}

        # GPTMail v1 公共 API：优先使用 X-API-Key 鉴权（若提供）
        if self.api_key:
            if "X-API-Key" not in headers and "x-api-key" not in headers:
                headers["X-API-Key"] = self.api_key

        # 近期版本：服务端可能支持/要求 token 鉴权；在已获取 token 后自动携带
        # 兼容策略：不覆盖调用方显式传入的 Authorization / X-Inbox-Token
        if self.inbox_token:
            if "Authorization" not in headers and "authorization" not in headers:
                headers["Authorization"] = f"Bearer {self.inbox_token}"
            if "X-Inbox-Token" not in headers and "x-inbox-token" not in headers:
                headers["X-Inbox-Token"] = self.inbox_token
        kwargs["headers"] = headers
        
        self._log("info", f"[HTTP] {method} {url}")
        if "json" in kwargs:
            self._log("info", f"[HTTP] Request body: {kwargs['json']}")

        try:
            res = self.session.request(
                method,
                url,
                proxies=self.proxies,
                verify=self.verify_ssl,
                timeout=kwargs.pop("timeout", 15),
                **kwargs,
            )
            self._log("info", f"[HTTP] Response: {res.status_code}")
            if res.content and res.status_code >= 400:
                try:
                    self._log("info", f"[HTTP] Response body: {res.text[:500]}")
                except Exception:
                    pass

            # 从响应中提取 auth.token/expires_at（若存在），用于后续请求
            self._try_update_auth_from_response(res)
            return res
        except Exception as e:
            self._log("error", f"[HTTP] Request failed: {e}")
            raise

    def warm_up(self) -> bool:
        """预热，获取必要的 Cookie"""
        try:
            self._log("info", "正在预热 (获取 Cookie)...")
            headers = {
                **self.common_headers,
                "Upgrade-Insecure-Requests": "1",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "cache-control": "no-cache",
                "pragma": "no-cache",
            }
            res = self._request("GET", self.home_url, headers=headers)
            if res.status_code == 200:
                # 新版可能要求 gm_sid（浏览器会话）
                gm_sid = self._get_cookie_value("gm_sid")
                if gm_sid:
                    self._log("info", "预热成功 (gm_sid 已获取)")
                    return True
                self._log("warning", "预热成功但未获取到 gm_sid，后续 API 可能返回 Browser session required")
        except Exception as e:
            self._log("error", f"预热失败: {e}")

        bridge_response = self._run_headless_bridge("warm_up")
        if bridge_response and self._get_cookie_value("gm_sid"):
            self._log("info", "headless warm-up acquired gm_sid")
            return True
        
        self._log("error", "预热失败")
        return False

    def _preflight_auth(self) -> None:
        """预取 auth token（若服务端已启用 token 鉴权）"""
        try:
            if self.inbox_token:
                return
            res = self._request("GET", f"{self.base_url}/stats", headers={"accept": "*/*"})
            if res.status_code == 200:
                try:
                    data = res.json() if res.content else {}
                    if isinstance(data, dict):
                        self._update_auth_from_json(data)
                except Exception:
                    return
        except Exception:
            return
        
    def register_account(self) -> bool:
        """获取临时邮箱地址"""
        def _extract_domain(email: str) -> str:
            try:
                return (email.rsplit("@", 1)[-1] or "").strip().lower()
            except Exception:
                return ""

        def _generate_email_once() -> Optional[str]:
            try:
                # 先预热
                if not self.warm_up():
                    self._log("error", "预热失败，无法获取邮箱")
                    return None

                # 近期版本：先访问 /stats 以获取 auth.token（若已启用）
                self._preflight_auth()

                self._log("info", "正在申请临时邮箱...")
                headers = {
                    **self.common_headers,
                    "content-type": "application/json"
                }
                
                res = self._request(
                    "GET",
                    f"{self.base_url}/generate-email",
                    headers=headers
                )
                
                if res.status_code == 401:
                    try:
                        payload = res.json() if res.content else {}
                    except Exception:
                        payload = {}
                    err = payload.get("error") if isinstance(payload, dict) else None
                    if isinstance(err, str) and "Browser session required" in err:
                        gm_sid = self._get_cookie_value("gm_sid")
                        self._log(
                            "error",
                            "服务端要求浏览器会话 (Browser session required)。"
                            f"当前 gm_sid={'set' if gm_sid else 'missing'}，"
                            f"inbox_token={'set' if self.inbox_token else 'missing'}。"
                            "可从浏览器抓包注入 gm_sid / X-Inbox-Token 后重试。",
                        )
                        return self._register_via_headless("browser session required")

                if res.status_code == 429:
                    reason = f"HTTP 429: {res.text[:200]}"
                    self._log("warning", f"HTTP register unavailable, {reason}")
                    return self._register_via_headless(reason)

                if res.status_code == 200:
                    data = res.json() if res.content else {}
                    if isinstance(data, dict):
                        self._update_auth_from_json(data)
                    if data.get("success") and data.get("data") and data["data"].get("email"):
                        return data["data"]["email"]
                    self._log("warning", f"HTTP register returned unexpected payload, switching to headless: {str(data)[:200]}")
                    return self._register_via_headless("unexpected HTTP payload")

                self._log("warning", f"HTTP register returned status {res.status_code}, switching to headless")
                return self._register_via_headless(f"HTTP {res.status_code}")
            except Exception as e:
                self._log("error", f"ChatGPT Mail 获取邮箱失败: {e}")
                return self._register_via_headless(f"exception: {e}")

        try:
            first_email = _generate_email_once()
            if not first_email:
                self._log("error", "ChatGPT Mail 获取邮箱失败")
                return False

            self.email = first_email
            domain = _extract_domain(first_email)

            # 如果该邮箱后缀历史成功率位于后半区：刷新并重新判定，最多刷新 3 次
            refresh_count = 0
            while domain and should_refresh_once_for_domain(domain) and refresh_count < 3:
                refresh_count += 1
                self._log("info", f"domain '{domain}' success-rate ranked low; refreshing ({refresh_count}/3)")
                refreshed_email = _generate_email_once()
                if not refreshed_email:
                    self._log("warning", "refresh failed; keeping current email")
                    break
                self.email = refreshed_email
                domain = _extract_domain(refreshed_email)
                self._log("info", f"ChatGPT Mail 获取邮箱成功(刷新后): {self.email}")

            self._log("info", f"ChatGPT Mail 获取邮箱成功: {self.email}")
            return True
        except Exception as e:
            self._log("error", f"ChatGPT Mail 获取邮箱失败: {e}")
            return False

        self._log("error", "ChatGPT Mail 获取邮箱失败")
        return False

    def login(self) -> bool:
        """登录（此服务不需要登录，直接返回 True）"""
        return self.email is not None

    def fetch_messages(self) -> list:
        """获取邮件列表"""
        if not self.email:
            return []

        if self._prefer_headless_fetch_messages:
            bridge_response = self._run_headless_bridge("fetch_messages")
            if bridge_response:
                emails = self._extract_emails_from_payload(bridge_response.get("result"))
                if emails:
                    self._log("info", f"headless fetched {len(emails)} emails")
                return emails
            self._log("error", "headless fetch_messages failed while in preferred-headless mode")
            return []

        try:
            from urllib.parse import quote
            encoded_email = quote(self.email)
            url = f"{self.base_url}/emails?email={encoded_email}"
            
            headers = {
                **self.common_headers,
                "accept": "*/*",
                "cache-control": "no-cache"
            }
            
            res = self._request("GET", url, headers=headers)

            if res.status_code == 401:
                try:
                    payload = res.json() if res.content else {}
                except Exception:
                    payload = {}
                error_message = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error_message, str) and "Browser session required" in error_message:
                    self._prefer_headless_fetch_messages = True
                    bridge_response = self._run_headless_bridge("fetch_messages")
                    if bridge_response:
                        emails = self._extract_emails_from_payload(bridge_response.get("result"))
                        if emails:
                            self._log("info", f"headless fetched {len(emails)} emails")
                        return emails
            
            if res.status_code == 429:
                self._prefer_headless_fetch_messages = True
                bridge_response = self._run_headless_bridge("fetch_messages")
                if bridge_response:
                    emails = self._extract_emails_from_payload(bridge_response.get("result"))
                    if emails:
                        self._log("info", f"headless fetched {len(emails)} emails")
                    return emails

            if res.status_code == 200:
                try:
                    data = res.json() if res.content else {}
                    if isinstance(data, dict):
                        self._update_auth_from_json(data)
                    if data.get("success") and data.get("data"):
                        emails = data["data"].get("emails", [])
                        if emails:
                            self._log("info", f"成功获取 {len(emails)} 封邮件")
                        else:
                            self._log("info", "邮箱暂无邮件")
                        return emails
                    else:
                        self._log("error", f"API 响应格式异常: {res.text[:200]}")
                        self._prefer_headless_fetch_messages = True
                        bridge_response = self._run_headless_bridge("fetch_messages")
                        if bridge_response:
                            emails = self._extract_emails_from_payload(bridge_response.get("result"))
                            if emails:
                                self._log("info", f"headless fetched {len(emails)} emails")
                            return emails
                except ValueError as e:
                    self._log("error", f"JSON 解析失败，响应内容: {res.text[:500]}")
        except Exception as e:
            self._log("error", f"获取邮件列表失败: {e}")
            self._prefer_headless_fetch_messages = True
            bridge_response = self._run_headless_bridge("fetch_messages")
            if bridge_response:
                emails = self._extract_emails_from_payload(bridge_response.get("result"))
                if emails:
                    self._log("info", f"headless fetched {len(emails)} emails")
                return emails
            
        bridge_response = self._run_headless_bridge("fetch_messages")
        if bridge_response:
            emails = self._extract_emails_from_payload(bridge_response.get("result"))
            if emails:
                self._log("info", f"headless fetched {len(emails)} emails")
            return emails
        return []

    def fetch_verification_code(self, since_time: Optional[datetime] = None) -> Optional[str]:
        """获取验证码"""
        if not self.email:
            return None

        try:
            self._log("info", "fetching verification code")
            
            # 计算时间阈值：当前时间 - 10秒
            current_timestamp = time.time()
            time_threshold = current_timestamp - 10
            
            messages = self.fetch_messages()
            
            if not messages:
                return None

            # 遍历邮件
            for msg in messages:
                # 使用 timestamp 字段进行时间过滤（只检查最近10秒内的邮件）
                msg_timestamp = msg.get("timestamp")
                # if msg_timestamp:
                #     if msg_timestamp < time_threshold:
                #         self._log("info", f"跳过旧邮件: timestamp={msg_timestamp} < {time_threshold}")
                #         continue
                
                # 额外的 since_time 过滤（如果提供）
                if since_time and msg.get("timestamp"):
                    try:
                        # 解析时间戳
                        msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
                        if msg_time < since_time:
                            continue
                    except Exception:
                        pass

                # 提取邮件内容
                subject = msg.get("subject") or ""
                html_content = msg.get("html_content") or ""
                text_content = msg.get("content") or ""
                
                # 记录邮件信息用于调试
                self._log("info", f"检查邮件: subject='{subject[:50] if subject else 'N/A'}', timestamp={msg_timestamp}")
                
                content = f"{subject} {html_content} {text_content}"
                
                # 提取验证码
                code = extract_verification_code(content)
                if code:
                    self._log("info", f"code found: {code} (from subject='{subject[:50] if subject else 'N/A'}')")
                    return code

            return None

        except Exception as e:
            self._log("error", f"fetch code failed: {e}")
            return None

    def poll_for_code(
        self,
        timeout: int = 120,
        interval: int = 3,
        since_time: Optional[datetime] = None,
    ) -> Optional[str]:
        """轮询获取验证码"""
        if not self.email:
            return None

        # 确保已经预热（获取 Cookie）
        if not self.session.cookies:
            self._log("info", "未检测到 Cookie，正在预热...")
            if not self.warm_up():
                self._log("error", "预热失败，无法获取验证码")
                return None

        self._log("info", f"开始监听邮箱 {self.email}，等待验证码...")

        started_at = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            code = self.fetch_verification_code(since_time=since_time)
            if code:
                return code

            elapsed = time.monotonic() - started_at
            if elapsed >= timeout:
                break

            remaining = timeout - elapsed
            time.sleep(min(interval, max(0, remaining)))

        self._log("error", "verification code timeout")
        return None

    def _log(self, level: str, message: str) -> None:
        if self.log_callback:
            try:
                self.log_callback(level, message)
            except Exception:
                pass
