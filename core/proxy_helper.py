"""
代理辅助模块 - 处理带认证的代理
Chrome 的 --proxy-server 不支持 http://user:pass@host:port 格式
需要使用扩展程序来处理代理认证
"""
import os
import zipfile
import tempfile
import random
from typing import Optional, Tuple, List
from urllib.parse import quote


def _split_proxy_list(raw: str) -> List[str]:
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def choose_random_proxy(raw: str) -> str:
    """
    从逗号分隔的代理列表中随机选择一个。
    raw 示例: "proxy1,proxy2,proxy3"
    返回: 单个代理字符串（若 raw 为空/无有效项则返回空字符串）
    """
    proxies = _split_proxy_list(raw)
    if not proxies:
        return ""
    return random.choice(proxies)


def parse_proxy(proxy_url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    解析代理URL

    支持格式：
    - http://user:pass@host:port
    - user:pass@host:port
    - host:port
    - host:port:username:password  （常见于部分代理商面板导出的格式）
    - http://host:port:username:password（部分代理商导出格式，带 scheme）

    Returns:
        (proxy_server, username, password) 元组
        - proxy_server: host:port 格式
        - username: 用户名（如果有）
        - password: 密码（如果有）
    """
    if not proxy_url:
        return None, None, None

    proxy_url = proxy_url.strip()
    if not proxy_url:
        return None, None, None

    # 移除协议前缀
    proxy_url = (
        proxy_url.replace("http://", "")
        .replace("https://", "")
        .replace("socks5://", "")
    )

    # 1) user:pass@host:port
    if "@" in proxy_url:
        auth_part, server_part = proxy_url.split("@", 1)

        if ":" in auth_part:
            username, password = auth_part.split(":", 1)
        else:
            username = auth_part
            password = ""

        return server_part, username, password

    # 2) host:port:username:password
    # 密码可能包含 ':'，因此只固定前 3 段，剩余全部并入 password
    parts = proxy_url.split(":")
    if len(parts) >= 4:
        host = parts[0].strip()
        port = parts[1].strip()
        username = parts[2]
        password = ":".join(parts[3:])

        # 端口必须为数字才认为是该格式，否则回退到 host:port
        if host and port.isdigit():
            return f"{host}:{port}", username, password

    # 3) host:port（或其它不带认证的直连形式）
    return proxy_url, None, None


def _detect_scheme(proxy_raw: str) -> str:
    raw = (proxy_raw or "").strip().lower()
    if raw.startswith("socks5://"):
        return "socks5"
    if raw.startswith("https://"):
        return "https"
    if raw.startswith("http://"):
        return "http"
    return "http"


def normalize_proxy_for_httpx(proxy_raw: str) -> str:
    """
    将各种代理格式标准化为 httpx 可用的代理 URL。

    支持：
    - http://user:pass@host:port
    - host:port
    - host:port:user:pass
    - http://host:port:user:pass
    - socks5://host:port[:user:pass]

    返回示例：
    - http://host:port
    - http://user:pass@host:port
    """
    if not proxy_raw:
        return ""
    proxy_raw = proxy_raw.strip()
    if not proxy_raw:
        return ""

    scheme = _detect_scheme(proxy_raw)

    server, username, password = parse_proxy(proxy_raw)
    if not server:
        return ""

    if username is None:
        return f"{scheme}://{server}"

    user_enc = quote(str(username), safe="")
    pass_enc = quote(str(password or ""), safe="")
    return f"{scheme}://{user_enc}:{pass_enc}@{server}"


def choose_random_httpx_proxy(raw: str) -> str:
    """
    从逗号分隔的代理列表中随机选一个，并规范化为 httpx 可用的代理 URL。

    支持示例：
    - http://p.webshare.io:80:mqctkwnq-rotate:pcwx9yuh72gn,http://p.webshare.io:80:mrxejnvh-rotate:8ri7r33duyft
    """
    picked = choose_random_proxy(raw)
    return normalize_proxy_for_httpx(picked)


def create_proxy_auth_extension(username: str, password: str, output_dir: str = None) -> str:
    """
    创建 Chrome 代理认证扩展

    Args:
        username: 代理用户名
        password: 代理密码
        output_dir: 输出目录（默认使用临时目录）

    Returns:
        扩展文件路径（.zip）
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="proxy-ext-")

    manifest_json = """
{
    "version": "1.0.0",
    "manifest_version": 2,
    "name": "Proxy Auth",
    "permissions": [
        "proxy",
        "tabs",
        "unlimitedStorage",
        "storage",
        "<all_urls>",
        "webRequest",
        "webRequestBlocking"
    ],
    "background": {
        "scripts": ["background.js"]
    },
    "minimum_chrome_version": "22.0.0"
}
"""

    background_js = """
var config = {
    mode: "fixed_servers",
    rules: {
        singleProxy: {
            scheme: "http",
            host: "%s",
            port: %s
        },
        bypassList: ["localhost"]
    }
};

chrome.proxy.settings.set({value: config, scope: "regular"}, function() {});

function callbackFn(details) {
    return {
        authCredentials: {
            username: "%s",
            password: "%s"
        }
    };
}

chrome.webRequest.onAuthRequired.addListener(
    callbackFn,
    {urls: ["<all_urls>"]},
    ['blocking']
);
""" % (
        username,
        password,
        username,
        password,
    )  # 这里需要替换代理服务器和端口

    ext_path = os.path.join(output_dir, "proxy_auth_extension.zip")

    with zipfile.ZipFile(ext_path, "w") as zf:
        zf.writestr("manifest.json", manifest_json)
        zf.writestr("background.js", background_js)

    return ext_path


def get_proxy_extension_path(proxy_server: str, username: str, password: str) -> str:
    """
    获取或创建代理认证扩展

    Args:
        proxy_server: 代理服务器（host:port）
        username: 用户名
        password: 密码

    Returns:
        扩展目录路径
    """
    ext_dir = tempfile.mkdtemp(prefix="proxy-auth-")

    if ":" in proxy_server:
        host, port = proxy_server.rsplit(":", 1)
    else:
        host = proxy_server
        port = "80"

    manifest_json = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Proxy Auth",
        "permissions": [
            "proxy",
            "tabs",
            "unlimitedStorage",
            "storage",
            "<all_urls>",
            "webRequest",
            "webRequestBlocking",
        ],
        "background": {"scripts": ["background.js"]},
        "minimum_chrome_version": "22.0.0",
    }

    background_js = f"""
var config = {{
    mode: "fixed_servers",
    rules: {{
        singleProxy: {{
            scheme: "http",
            host: "{host}",
            port: parseInt({port})
        }},
        bypassList: ["localhost"]
    }}
}};

chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});

function callbackFn(details) {{
    return {{
        authCredentials: {{
            username: "{username}",
            password: "{password}"
        }}
    }};
}}

chrome.webRequest.onAuthRequired.addListener(
    callbackFn,
    {{urls: ["<all_urls>"]}},
    ['blocking']
);
"""

    import json

    with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest_json, f)

    with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
        f.write(background_js)

    return ext_dir