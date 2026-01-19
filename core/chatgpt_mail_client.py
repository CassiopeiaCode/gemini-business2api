import time
from typing import Optional
from datetime import datetime

import requests

from core.mail_utils import extract_verification_code


class ChatGPTMailClient:
    """ChatGPT.org.uk 临时邮箱客户端"""

    def __init__(
        self,
        base_url: str = "https://mail.chatgpt.org.uk",
        proxy: str = "",
        verify_ssl: bool = True,
        log_callback=None,
    ) -> None:
        self.home_url = base_url.rstrip("/")
        self.base_url = f"{self.home_url}/api"
        self.verify_ssl = verify_ssl
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.log_callback = log_callback

        self.email: Optional[str] = None
        self.session = requests.Session()  # 使用 Session 自动管理 Cookie
        
        # 通用请求头
        self.common_headers = {
            "sec-ch-ua": '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
            "Origin": self.home_url,
            "Referer": f"{self.home_url}/",
        }

    def set_credentials(self, email: str, password: Optional[str] = None) -> None:
        """设置邮箱凭证（此服务不需要密码）"""
        self.email = email

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """发送请求并打印详细日志"""
        headers = kwargs.pop("headers", None) or {}
        # 合并通用请求头
        headers = {**self.common_headers, **headers}
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
                "Upgrade-Insecure-Requests": "1"
            }
            res = self._request("GET", self.home_url, headers=headers)
            if res.status_code == 200:
                self._log("info", "预热成功")
                return True
        except Exception as e:
            self._log("error", f"预热失败: {e}")
            return False
        
        self._log("error", "预热失败")
        return False

    def register_account(self) -> bool:
        """获取临时邮箱地址"""
        try:
            # 先预热
            if not self.warm_up():
                self._log("error", "预热失败，无法获取邮箱")
                return False

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
            
            if res.status_code == 200:
                data = res.json() if res.content else {}
                if data.get("success") and data.get("data") and data["data"].get("email"):
                    self.email = data["data"]["email"]
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
            
            if res.status_code == 200:
                try:
                    data = res.json() if res.content else {}
                    if data.get("success") and data.get("data"):
                        emails = data["data"].get("emails", [])
                        if emails:
                            self._log("info", f"成功获取 {len(emails)} 封邮件")
                        else:
                            self._log("info", "邮箱暂无邮件")
                        return emails
                    else:
                        self._log("error", f"API 响应格式异常: {res.text[:200]}")
                except ValueError as e:
                    self._log("error", f"JSON 解析失败，响应内容: {res.text[:500]}")
        except Exception as e:
            self._log("error", f"获取邮件列表失败: {e}")
            
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

        max_retries = max(1, timeout // interval)
        self._log("info", f"开始监听邮箱 {self.email}，等待验证码...")

        for i in range(1, max_retries + 1):
            code = self.fetch_verification_code(since_time=since_time)
            if code:
                return code

            if i < max_retries:
                time.sleep(interval)

        self._log("error", "verification code timeout")
        return None

    def _log(self, level: str, message: str) -> None:
        if self.log_callback:
            try:
                self.log_callback(level, message)
            except Exception:
                pass