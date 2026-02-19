"""
Gemini自动化登录模块（使用 DrissionPage + fingerprint-chromium）
复用 DrissionPage 的自动化逻辑，但使用 fingerprint-chromium 作为浏览器二进制
"""
import random
import string
import time
import os
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

from DrissionPage import ChromiumPage, ChromiumOptions

from .proxy_helper import parse_proxy, get_proxy_extension_path
from .browser_failure_tracker import record_browser_failure


# 常量
AUTH_HOME_URL = "https://auth.business.gemini.google/"
DEFAULT_XSRF_TOKEN = "KdLRzKwwBTD5wo8nUollAbY6cW0"


class GeminiAutomationFP:
    """Gemini自动化登录（DrissionPage + fingerprint-chromium）"""

    def __init__(
        self,
        user_agent: str = "",
        proxy: str = "",
        headless: bool = True,
        timeout: int = 60,
        log_callback=None,
        fp_chrome_path: str = "",
    ) -> None:
        # fingerprint-chromium 使用浏览器默认 UA，不设置自定义 UA
        self.user_agent = ""
        self.proxy = proxy
        self.headless = headless
        self.timeout = timeout
        self.log_callback = log_callback
        self.fp_chrome_path = fp_chrome_path or self._find_fp_chrome()
        self.fingerprint_seed = self._generate_fingerprint_seed()

    def _find_fp_chrome(self) -> str:
        """查找 fingerprint-chromium 可执行文件"""
        # Windows 常见路径
        windows_paths = [
            r"C:\Program Files\fingerprint-chromium\chrome.exe",
            r"C:\Program Files (x86)\fingerprint-chromium\chrome.exe",
            os.path.expanduser("~/AppData/Local/fingerprint-chromium/chrome.exe"),
        ]
        
        # Linux 常见路径
        linux_paths = [
            # Docker 容器内的路径（优先）
            "/app/ungoogled-chromium-142.0.7444.175-1-x86_64_linux/chrome",
            # 常见安装路径
            "/usr/bin/fingerprint-chromium",
            "/usr/local/bin/fingerprint-chromium",
            os.path.expanduser("~/fingerprint-chromium/chrome"),
        ]
        
        # macOS 常见路径
        mac_paths = [
            "/Applications/fingerprint-chromium.app/Contents/MacOS/fingerprint-chromium",
            os.path.expanduser("~/Applications/fingerprint-chromium.app/Contents/MacOS/fingerprint-chromium"),
        ]
        
        # 尝试所有路径
        for path in windows_paths + linux_paths + mac_paths:
            if os.path.exists(path):
                return path
        
        # 尝试从环境变量获取
        env_path = os.getenv("FP_CHROME_PATH")
        if env_path and os.path.exists(env_path):
            return env_path
        
        # 返回空字符串，让 DrissionPage 使用默认 Chrome
        return ""

    def _generate_fingerprint_seed(self) -> int:
        """生成随机指纹种子（32位整数）"""
        return random.randint(1000000, 2147483647)

    def login_and_extract(self, email: str, mail_client) -> dict:
        """执行登录并提取配置"""
        page = None
        user_data_dir = None
        try:
            page = self._create_page()
            user_data_dir = getattr(page, 'user_data_dir', None)
            return self._run_flow(page, email, mail_client)
        except Exception as exc:
            error_msg = str(exc)
            self._log("error", f"automation error: {error_msg}")

            # 检测浏览器连接/启动错误：计入全局失败次数，超过阈值将直接 SystemExit
            if "浏览器无法链接" in error_msg or "remote-debugging-port" in error_msg:
                fail_count = record_browser_failure()
                self._log("warning", f"browser startup failure recorded (global_count={fail_count})")
                self._cleanup_drissionpage_cache()

            return {"success": False, "error": error_msg}
        finally:
            if page:
                try:
                    page.quit()
                except Exception:
                    pass
            self._cleanup_user_data(user_data_dir)

    def _create_page(self) -> ChromiumPage:
        """创建浏览器页面（使用 fingerprint-chromium）"""
        options = ChromiumOptions()
        
        # 设置 fingerprint-chromium 二进制路径
        if self.fp_chrome_path:
            options.set_browser_path(self.fp_chrome_path)
            self._log("info", f"using fingerprint-chromium: {self.fp_chrome_path}")
        else:
            self._log("warning", "fingerprint-chromium path not found, using default chrome")
        
        # 基础参数
        options.set_argument("--incognito")
        options.set_argument("--no-sandbox")
        options.set_argument("--disable-dev-shm-usage")
        options.set_argument("--disable-setuid-sandbox")
        options.set_argument("--disable-blink-features=AutomationControlled")
        options.set_argument("--window-size=1280,800")

        # 降低内存/后台开销（对登录流程通常无副作用）
        options.set_argument("--no-first-run")
        options.set_argument("--no-default-browser-check")
        options.set_argument("--disable-background-networking")
        options.set_argument("--disable-background-timer-throttling")
        options.set_argument("--disable-backgrounding-occluded-windows")
        options.set_argument("--disable-renderer-backgrounding")
        options.set_argument("--disable-sync")
        options.set_argument("--disable-translate")
        options.set_argument("--metrics-recording-only")
        options.set_argument("--disable-features=TranslateUI,BlinkGenPropertyTrees")

        # 可选：禁用图片加载以显著降低内存/带宽（可通过环境变量关闭）
        #   BROWSER_DISABLE_IMAGES=1 (默认) 禁用图片
        #   BROWSER_DISABLE_IMAGES=0 启用图片
        if os.getenv("BROWSER_DISABLE_IMAGES", "1") == "1":
            options.set_argument("--blink-settings=imagesEnabled=false")
            options.set_pref("profile.managed_default_content_settings.images", 2)
            options.set_pref("profile.default_content_setting_values.images", 2)

        # fingerprint-chromium 使用浏览器默认 UA，不设置自定义 UA
        # options.set_user_agent(self.user_agent)

        # fingerprint-chromium 指纹参数（核心）
        options.set_argument(f"--fingerprint={self.fingerprint_seed}")
        options.set_argument("--fingerprint-platform=windows")
        
        # 语言设置（确保使用中文界面）
        options.set_argument("--lang=zh-CN")
        options.set_pref("intl.accept_languages", "zh-CN,zh")

        # 代理设置
        if self.proxy:
            proxy_server, username, password = parse_proxy(self.proxy)
            
            if username and password:
                # 带认证的代理：使用扩展程序
                try:
                    proxy_extension_dir = get_proxy_extension_path(proxy_server, username, password)
                    options.set_argument(f"--load-extension={proxy_extension_dir}")
                    self._log("info", f"using proxy with auth: {proxy_server}")
                except Exception as e:
                    self._log("warning", f"failed to create proxy extension: {e}, trying direct proxy")
                    options.set_argument(f"--proxy-server={proxy_server}")
            else:
                # 无认证的代理：直接使用
                options.set_argument(f"--proxy-server={proxy_server}")

        if self.headless:
            # 使用新版无头模式
            options.set_argument("--headless=new")
            options.set_argument("--disable-gpu")
            options.set_argument("--no-first-run")
            options.set_argument("--disable-extensions")
            # 反检测参数
            options.set_argument("--disable-infobars")
            options.set_argument("--enable-features=NetworkService,NetworkServiceInProcess")

        options.auto_port()
        page = ChromiumPage(options)
        page.set.timeouts(self.timeout)

        # fingerprint-chromium 自带反检测，不需要注入脚本
        # 浏览器指纹随机化由 --fingerprint 参数控制
        
        self._log("info", f"fingerprint-chromium started with seed: {self.fingerprint_seed}")
        return page

    def _run_flow(self, page, email: str, mail_client) -> dict:
        """执行登录流程（与原 DrissionPage 逻辑一致）"""

        # 记录开始时间，用于邮件时间过滤
        send_time = datetime.now()

        # Step 1: 导航到首页并设置 Cookie
        self._log("info", f"navigating to login page for {email}")

        page.get(AUTH_HOME_URL, timeout=self.timeout)
        time.sleep(2)

        # 设置两个关键 Cookie
        try:
            page.set.cookies({
                "name": "__Host-AP_SignInXsrf",
                "value": DEFAULT_XSRF_TOKEN,
                "url": AUTH_HOME_URL,
                "path": "/",
                "secure": True,
            })
            # 添加 reCAPTCHA Cookie
            page.set.cookies({
                "name": "_GRECAPTCHA",
                "value": "09ABCL...",
                "url": "https://google.com",
                "path": "/",
                "secure": True,
            })
        except Exception as e:
            self._log("warning", f"failed to set cookies: {e}")

        login_hint = quote(email, safe="")
        login_url = f"https://auth.business.gemini.google/login/email?continueUrl=https%3A%2F%2Fbusiness.gemini.google%2F&loginHint={login_hint}&xsrfToken={DEFAULT_XSRF_TOKEN}"
        page.get(login_url, timeout=self.timeout)
        time.sleep(5)

        # Step 2: 检查当前页面状态
        current_url = page.url
        has_business_params = "business.gemini.google" in current_url and "csesidx=" in current_url and "/cid/" in current_url

        if has_business_params:
            return self._extract_config(page, email)

        # Step 3: 点击发送验证码按钮
        self._log("info", "clicking send verification code button")
        if not self._click_send_code_button(page):
            self._log("error", "send code button not found")
            self._save_screenshot(page, "send_code_button_missing")
            return {"success": False, "error": "send code button not found"}

        # Step 4: 等待验证码输入框出现
        code_input = self._wait_for_code_input(page)
        if not code_input:
            self._log("error", "code input not found")
            self._save_screenshot(page, "code_input_missing")
            return {"success": False, "error": "code input not found"}

        # Step 5: 轮询邮件获取验证码（10秒超时，最多重发3次，每次等待30秒）
        self._log("info", "polling for verification code (10s timeout)")
        code = mail_client.poll_for_code(timeout=10, interval=2, since_time=send_time)
        
        # 如果10秒内没收到，尝试重发最多3次
        max_resend_attempts = 3
        for attempt in range(max_resend_attempts):
            if code:
                break
                
            self._log("warning", f"verification code not received in 10s, resending ({attempt + 1}/{max_resend_attempts})")
            
            # 更新发送时间（在点击按钮之前记录）
            send_time = datetime.now()
            
            # 点击重新发送按钮
            if not self._click_resend_code_button(page):
                self._log("error", "resend button not found")
                self._save_screenshot(page, "resend_button_missing")
                return {"success": False, "error": "resend button not found"}
            
            self._log("info", f"resend button clicked, waiting 30s for new code")
            # 等待10秒接收新验证码
            code = mail_client.poll_for_code(timeout=10, interval=2, since_time=send_time)
        
        if not code:
            self._log("error", f"verification code timeout after {max_resend_attempts} resend attempts")
            self._save_screenshot(page, "code_timeout_final")
            return {"success": False, "error": f"verification code timeout after {max_resend_attempts} resends"}

        self._log("info", f"code received: {code}")

        # Step 6: 输入验证码并提交
        code_input = page.ele("css:input[jsname='ovqh0b']", timeout=3) or \
                     page.ele("css:input[type='tel']", timeout=2)

        if not code_input:
            self._log("error", "code input expired")
            return {"success": False, "error": "code input expired"}

        self._log("info", "inputting verification code")
        code_input.input(code, clear=True)
        time.sleep(0.5)

        verify_btn = page.ele("css:button[jsname='XooR8e']", timeout=3)
        if verify_btn:
            self._log("info", "clicking verify button (method 1)")
            verify_btn.click()
        else:
            verify_btn = self._find_verify_button(page)
            if verify_btn:
                self._log("info", "clicking verify button (method 2)")
                verify_btn.click()
            else:
                self._log("info", "pressing enter to submit")
                code_input.input("\n")

        # Step 7: 等待页面自动重定向
        self._log("info", "waiting for auto-redirect after verification")
        time.sleep(12)

        # 记录当前 URL 状态
        current_url = page.url
        self._log("info", f"current URL after verification: {current_url}")

        # 检查是否还停留在验证码页面
        if "verify-oob-code" in current_url:
            self._log("error", "verification code submission failed, still on verification page")
            self._save_screenshot(page, "verification_submit_failed")
            return {"success": False, "error": "verification code submission failed"}

        # Step 8: 处理协议页面（如果有）
        self._handle_agreement_page(page)

        # Step 9: 检查是否已经在正确的页面
        current_url = page.url
        has_business_params = "business.gemini.google" in current_url and "csesidx=" in current_url and "/cid/" in current_url

        if has_business_params:
            self._log("info", "already on business page with parameters")
            return self._extract_config(page, email)

        # Step 10: 如果不在正确的页面，尝试导航
        if "business.gemini.google" not in current_url:
            self._log("info", "navigating to business page")
            page.get("https://business.gemini.google/", timeout=self.timeout)
            time.sleep(5)
            current_url = page.url
            self._log("info", f"URL after navigation: {current_url}")

        # Step 11: 检查是否需要设置用户名
        if "cid" not in page.url:
            if self._handle_username_setup(page):
                time.sleep(5)

        # Step 12: 等待 URL 参数生成
        self._log("info", "waiting for URL parameters")
        if not self._wait_for_business_params(page):
            self._log("warning", "URL parameters not generated, trying refresh")
            page.refresh()
            time.sleep(5)
            if not self._wait_for_business_params(page):
                self._log("error", "URL parameters generation failed")
                current_url = page.url
                self._log("error", f"final URL: {current_url}")
                self._save_screenshot(page, "params_missing")
                return {"success": False, "error": "URL parameters not found"}

        # Step 13: 提取配置
        self._log("info", "login success")
        return self._extract_config(page, email)

    def _click_send_code_button(self, page) -> bool:
        """点击发送验证码按钮（如果需要）"""
        time.sleep(2)

        # 方法1: 直接通过ID查找
        direct_btn = page.ele("#sign-in-with-email", timeout=5)
        if direct_btn:
            try:
                direct_btn.click()
                return True
            except Exception:
                pass

        # 方法2: 通过关键词查找
        keywords = ["通过电子邮件发送验证码", "通过电子邮件发送", "email", "Email", "Send code", "Send verification", "Verification code"]
        try:
            buttons = page.eles("tag:button")
            for btn in buttons:
                text = (btn.text or "").strip()
                if text and any(kw in text for kw in keywords):
                    try:
                        btn.click()
                        return True
                    except Exception:
                        pass
        except Exception:
            pass

        # 检查是否已经在验证码输入页面
        code_input = page.ele("css:input[jsname='ovqh0b']", timeout=2) or page.ele("css:input[name='pinInput']", timeout=1)
        if code_input:
            return True

        return False

    def _wait_for_code_input(self, page, timeout: int = 30):
        """等待验证码输入框出现"""
        selectors = [
            "css:input[jsname='ovqh0b']",
            "css:input[type='tel']",
            "css:input[name='pinInput']",
            "css:input[autocomplete='one-time-code']",
        ]
        for _ in range(timeout // 2):
            for selector in selectors:
                try:
                    el = page.ele(selector, timeout=1)
                    if el:
                        return el
                except Exception:
                    continue
            time.sleep(2)
        return None

    def _find_verify_button(self, page):
        """查找验证按钮（排除重新发送按钮）"""
        try:
            buttons = page.eles("tag:button")
            for btn in buttons:
                text = (btn.text or "").strip().lower()
                if text and "重新" not in text and "发送" not in text and "resend" not in text and "send" not in text:
                    return btn
        except Exception:
            pass
        return None

    def _click_resend_code_button(self, page) -> bool:
        """点击重新发送验证码按钮"""
        time.sleep(2)

        try:
            buttons = page.eles("tag:button")
            for btn in buttons:
                text = (btn.text or "").strip().lower()
                if text and ("重新" in text or "resend" in text):
                    try:
                        self._log("info", f"found resend button: {text}")
                        btn.click()
                        time.sleep(2)
                        return True
                    except Exception:
                        pass
        except Exception:
            pass

        return False

    def _handle_agreement_page(self, page) -> None:
        """处理协议页面"""
        if "/admin/create" in page.url:
            agree_btn = page.ele("css:button.agree-button", timeout=5)
            if agree_btn:
                agree_btn.click()
                time.sleep(2)

    def _wait_for_business_params(self, page, timeout: int = 30) -> bool:
        """等待业务页面参数生成（csesidx 和 cid）"""
        for _ in range(timeout):
            url = page.url
            if "csesidx=" in url and "/cid/" in url:
                self._log("info", f"business params ready: {url}")
                return True
            time.sleep(1)
        return False

    def _handle_username_setup(self, page) -> bool:
        """处理用户名设置页面"""
        current_url = page.url

        if "auth.business.gemini.google/login" in current_url:
            return False

        selectors = [
            "css:input[type='text']",
            "css:input[name='displayName']",
            "css:input[aria-label*='用户名' i]",
            "css:input[aria-label*='display name' i]",
        ]

        username_input = None
        for selector in selectors:
            try:
                username_input = page.ele(selector, timeout=2)
                if username_input:
                    break
            except Exception:
                continue

        if not username_input:
            return False

        suffix = "".join(random.choices(string.ascii_letters + string.digits, k=3))
        username = f"Test{suffix}"

        try:
            username_input.click()
            time.sleep(0.2)
            username_input.clear()
            username_input.input(username)
            time.sleep(0.3)

            buttons = page.eles("tag:button")
            submit_btn = None
            for btn in buttons:
                text = (btn.text or "").strip().lower()
                if any(kw in text for kw in ["确认", "提交", "继续", "submit", "continue", "confirm", "save", "保存", "下一步", "next"]):
                    submit_btn = btn
                    break

            if submit_btn:
                submit_btn.click()
            else:
                username_input.input("\n")

            time.sleep(5)
            return True
        except Exception:
            return False

    def _extract_config(self, page, email: str) -> dict:
        """提取配置"""
        try:
            if "cid/" not in page.url:
                page.get("https://business.gemini.google/", timeout=self.timeout)
                time.sleep(3)

            url = page.url
            if "cid/" not in url:
                return {"success": False, "error": "cid not found"}

            config_id = url.split("cid/")[1].split("?")[0].split("/")[0]
            csesidx = url.split("csesidx=")[1].split("&")[0] if "csesidx=" in url else ""

            cookies = page.cookies()
            ses = next((c["value"] for c in cookies if c["name"] == "__Secure-C_SES"), None)
            host = next((c["value"] for c in cookies if c["name"] == "__Host-C_OSES"), None)

            ses_obj = next((c for c in cookies if c["name"] == "__Secure-C_SES"), None)
            if ses_obj and "expiry" in ses_obj:
                expires_at = datetime.fromtimestamp(ses_obj["expiry"] - 43200).strftime("%Y-%m-%d %H:%M:%S")
            else:
                expires_at = (datetime.now() + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")

            config = {
                "id": email,
                "csesidx": csesidx,
                "config_id": config_id,
                "secure_c_ses": ses,
                "host_c_oses": host,
                "expires_at": expires_at,
            }
            return {"success": True, "config": config}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _save_screenshot(self, page, name: str) -> None:
        """保存截图"""
        try:
            screenshot_dir = os.path.join("data", "automation")
            os.makedirs(screenshot_dir, exist_ok=True)
            path = os.path.join(screenshot_dir, f"{name}_{int(time.time())}.png")
            page.get_screenshot(path=path)
        except Exception:
            pass

    def _log(self, level: str, message: str) -> None:
        """记录日志"""
        if self.log_callback:
            try:
                self.log_callback(level, message)
            except Exception:
                pass

    def _cleanup_user_data(self, user_data_dir: Optional[str]) -> None:
        """清理浏览器用户数据目录"""
        if not user_data_dir:
            return
        try:
            import shutil
            if os.path.exists(user_data_dir):
                shutil.rmtree(user_data_dir, ignore_errors=True)
        except Exception:
            pass

    def _cleanup_drissionpage_cache(self) -> None:
        """清理 DrissionPage 缓存目录并尝试终止残留浏览器进程（连接失败时调用）"""
        import shutil
        import subprocess
        import time
        import shutil as _shutil

        def _run_if_exists(cmd: list[str]) -> None:
            exe = cmd[0]
            if _shutil.which(exe) is None:
                return
            subprocess.run(cmd, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=False)

        # 1) 终止浏览器/驱动进程（按平台处理）
        try:
            self._log("warning", "killing all chrome/chromium processes")
            if os.name == "nt":
                _run_if_exists(["taskkill", "/F", "/T", "/IM", "chrome.exe"])
                _run_if_exists(["taskkill", "/F", "/T", "/IM", "chromium.exe"])
            else:
                _run_if_exists(["pkill", "-9", "chrome"])
                _run_if_exists(["pkill", "-9", "chromium"])
                _run_if_exists(["killall", "-9", "chrome"])
                _run_if_exists(["killall", "-9", "chromium"])
            time.sleep(1)
            self._log("info", "chrome/chromium processes killed")
        except Exception as e:
            self._log("warning", f"failed to kill chrome processes: {e}")

        try:
            self._log("warning", "killing all chromedriver processes")
            if os.name == "nt":
                _run_if_exists(["taskkill", "/F", "/T", "/IM", "chromedriver.exe"])
            else:
                _run_if_exists(["pkill", "-9", "chromedriver"])
                _run_if_exists(["killall", "-9", "chromedriver"])
            time.sleep(0.5)
            self._log("info", "chromedriver processes killed")
        except Exception as e:
            self._log("warning", f"failed to kill chromedriver processes: {e}")

        # 2) 清理 DrissionPage 缓存目录（Linux: /tmp；Windows: %TEMP%）
        try:
            candidates = [
                "/tmp/DrissionPage",
                os.path.join(os.getenv("TEMP", ""), "DrissionPage"),
            ]
            for drissionpage_dir in candidates:
                if drissionpage_dir and os.path.exists(drissionpage_dir):
                    self._log("warning", f"cleaning up DrissionPage cache: {drissionpage_dir}")
                    shutil.rmtree(drissionpage_dir, ignore_errors=True)
                    self._log("info", "DrissionPage cache cleaned successfully")
        except Exception as e:
            self._log("warning", f"failed to clean DrissionPage cache: {e}")

        # 3) 记录残留进程（仅用于诊断）
        try:
            if os.name != "nt":
                result = subprocess.run(
                    ["pgrep", "-f", "chrome"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.stdout.strip():
                    self._log("warning", f"remaining chrome processes: {result.stdout.strip()}")
        except Exception:
            pass

    @staticmethod
    def _get_ua() -> str:
        """生成随机User-Agent"""
        v = random.choice(["120.0.0.0", "121.0.0.0", "122.0.0.0"])
        return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36"