"""
代理辅助模块 - 处理带认证的代理
Chrome 的 --proxy-server 不支持 http://user:pass@host:port 格式
需要使用扩展程序来处理代理认证
"""
import os
import zipfile
import tempfile
from typing import Optional, Tuple


def parse_proxy(proxy_url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    解析代理URL
    
    Args:
        proxy_url: 代理URL，格式如 http://user:pass@host:port 或 host:port
        
    Returns:
        (proxy_server, username, password) 元组
        - proxy_server: host:port 格式
        - username: 用户名（如果有）
        - password: 密码（如果有）
    """
    if not proxy_url:
        return None, None, None
    
    # 移除协议前缀
    proxy_url = proxy_url.replace('http://', '').replace('https://', '').replace('socks5://', '')
    
    # 检查是否包含认证信息
    if '@' in proxy_url:
        # 格式: user:pass@host:port
        auth_part, server_part = proxy_url.split('@', 1)
        
        if ':' in auth_part:
            username, password = auth_part.split(':', 1)
        else:
            username = auth_part
            password = ''
        
        return server_part, username, password
    else:
        # 格式: host:port
        return proxy_url, None, None


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
        output_dir = tempfile.mkdtemp(prefix='proxy-ext-')
    
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
""" % (username, password, username, password)  # 这里需要替换代理服务器和端口
    
    # 创建扩展文件
    ext_path = os.path.join(output_dir, 'proxy_auth_extension.zip')
    
    with zipfile.ZipFile(ext_path, 'w') as zf:
        zf.writestr('manifest.json', manifest_json)
        zf.writestr('background.js', background_js)
    
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
    # 创建临时目录
    ext_dir = tempfile.mkdtemp(prefix='proxy-auth-')
    
    # 解析 host 和 port
    if ':' in proxy_server:
        host, port = proxy_server.rsplit(':', 1)
    else:
        host = proxy_server
        port = '80'
    
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
            "webRequestBlocking"
        ],
        "background": {
            "scripts": ["background.js"]
        },
        "minimum_chrome_version": "22.0.0"
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
    
    # 写入文件
    import json
    with open(os.path.join(ext_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest_json, f)
    
    with open(os.path.join(ext_dir, 'background.js'), 'w', encoding='utf-8') as f:
        f.write(background_js)
    
    return ext_dir