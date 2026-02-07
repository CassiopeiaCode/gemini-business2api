"""Google API交互模块

负责与Google Gemini Business API的所有交互操作
"""
import asyncio
import base64
import json
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, List

import httpx
from fastapi import HTTPException

if TYPE_CHECKING:
    from main import AccountManager

logger = logging.getLogger(__name__)

# Google API 基础URL
GEMINI_API_BASE = "https://biz-discoveryengine.googleapis.com/v1alpha"


def get_common_headers(jwt: str, user_agent: str) -> dict:
    """生成通用请求头"""
    return {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "authorization": f"Bearer {jwt}",
        "content-type": "application/json",
        "origin": "https://business.gemini.google",
        "referer": "https://business.gemini.google/",
        "user-agent": user_agent,
        "x-server-timeout": "1800",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
    }


async def make_request_with_jwt_retry(
    account_mgr: "AccountManager",
    method: str,
    url: str,
    http_client: httpx.AsyncClient,
    user_agent: str,
    request_id: str = "",
    **kwargs
) -> httpx.Response:
    """通用HTTP请求，自动处理JWT过期重试

    Args:
        account_mgr: AccountManager实例
        method: HTTP方法 (GET/POST)
        url: 请求URL
        http_client: httpx客户端
        user_agent: User-Agent字符串
        request_id: 请求ID（用于日志）
        **kwargs: 传递给httpx的其他参数（如json, headers等）

    Returns:
        httpx.Response对象
    """
    jwt = await account_mgr.get_jwt(request_id)
    headers = get_common_headers(jwt, user_agent)

    # 合并用户提供的headers（如果有）
    extra_headers = kwargs.pop("headers", None)
    if extra_headers:
        headers.update(extra_headers)

    # 发起请求
    if method.upper() == "GET":
        resp = await http_client.get(url, headers=headers, **kwargs)
    elif method.upper() == "POST":
        resp = await http_client.post(url, headers=headers, **kwargs)
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")

    # 如果401，刷新JWT后重试一次
    if resp.status_code == 401:
        jwt = await account_mgr.get_jwt(request_id)
        headers = get_common_headers(jwt, user_agent)
        if extra_headers:
            headers.update(extra_headers)

        if method.upper() == "GET":
            resp = await http_client.get(url, headers=headers, **kwargs)
        elif method.upper() == "POST":
            resp = await http_client.post(url, headers=headers, **kwargs)

    return resp


async def create_google_session(
    account_manager: "AccountManager",
    http_client: httpx.AsyncClient,
    user_agent: str,
    request_id: str = ""
) -> str:
    """创建Google Session"""
    jwt = await account_manager.get_jwt(request_id)
    headers = get_common_headers(jwt, user_agent)
    body = {
        "configId": account_manager.config.config_id,
        "additionalParams": {"token": "-"},
        "createSessionRequest": {
            "session": {"name": "", "displayName": ""}
        }
    }

    req_tag = f"[req_{request_id}] " if request_id else ""
    r = await http_client.post(
        f"{GEMINI_API_BASE}/locations/global/widgetCreateSession",
        headers=headers,
        json=body,
    )
    if r.status_code != 200:
        logger.error(f"[SESSION] [{account_manager.config.account_id}] {req_tag}Session 创建失败: {r.status_code}")
        raise HTTPException(r.status_code, "createSession failed")
    sess_name = r.json()["session"]["name"]
    logger.info(f"[SESSION] [{account_manager.config.account_id}] {req_tag}创建成功: {sess_name[-12:]}")
    return sess_name


async def upload_context_file(
    session_name: str,
    mime_type: str,
    base64_content: str,
    account_manager: "AccountManager",
    http_client: httpx.AsyncClient,
    user_agent: str,
    request_id: str = "",
) -> str:
    """上传文件到指定 Session，返回 fileId

    兼容 Model Armor 拦截的降级策略：
    - 第一次按原样上传
    - 若被 Model Armor block，则将内容再做一次 base64 编码后重试一次
    - 仍失败则抛出结构化的 model_armor_violation 错误
    """

    def _extract_model_armor_violation(payload: dict) -> dict | None:
        """
        解析 Model Armor 拦截（Prompt Injection / Jailbreak 等）错误。
        返回结构化信息，供上层识别为“不可重试”并向下游透传更清晰的提示。
        """
        err = (payload or {}).get("error") or {}
        details = err.get("details") or []
        if not isinstance(details, list):
            return None
        for item in details:
            if not isinstance(item, dict):
                continue
            if item.get("reason") != "MODEL_ARMOR_VIOLATION":
                continue
            meta = item.get("metadata") or {}
            return {
                "upstream_reason": "MODEL_ARMOR_VIOLATION",
                "upstream_domain": item.get("domain"),
                "upstream_details": meta.get("details"),
            }
        return None

    async def _do_upload(file_contents_b64: str) -> httpx.Response:
        jwt = await account_manager.get_jwt(request_id)
        headers = get_common_headers(jwt, user_agent)

        # 生成随机文件名
        ext = mime_type.split("/")[-1] if "/" in mime_type else "bin"
        file_name = f"upload_{int(time.time())}_{uuid.uuid4().hex[:6]}.{ext}"

        body = {
            "configId": account_manager.config.config_id,
            "additionalParams": {"token": "-"},
            "addContextFileRequest": {
                "name": session_name,
                "fileName": file_name,
                "mimeType": mime_type,
                "fileContents": file_contents_b64,
            },
        }

        return await http_client.post(
            f"{GEMINI_API_BASE}/locations/global/widgetAddContextFile",
            headers=headers,
            json=body,
        )

    req_tag = f"[req_{request_id}] " if request_id else ""

    # 第一次：按原样上传
    r = await _do_upload(base64_content)

    # Model Armor 拦截时，降级：再 base64 一次重试
    if r.status_code == 400:
        try:
            payload = json.loads(r.text or "{}")
        except Exception:
            payload = None

        upstream_message = str(((payload or {}).get("error") or {}).get("message") or "")
        if payload and _extract_model_armor_violation(payload):
            try:
                retry_content = base64.b64encode((base64_content or "").encode("utf-8")).decode("utf-8")
            except Exception:
                retry_content = ""

            if retry_content:
                logger.warning(
                    f"[FILE] [{account_manager.config.account_id}] {req_tag}"
                    f"检测到 Model Armor 拦截，尝试 base64 降级重试一次",
                )
                r = await _do_upload(retry_content)

    if r.status_code != 200:
        logger.error(f"[FILE] [{account_manager.config.account_id}] {req_tag}文件上传失败: {r.status_code}")
        error_text = r.text

        # 尽量结构化解析上游错误，便于上层做“是否重试”的决策，也便于下游理解。
        payload = None
        upstream_message = ""
        upstream_code = None
        try:
            payload = json.loads(r.text or "{}")
            upstream_message = str((payload.get("error") or {}).get("message") or "")
            upstream_code = (payload.get("error") or {}).get("code")
        except Exception:
            payload = None

        if r.status_code == 400 and payload:
            if "Unsupported file type" in upstream_message:
                bad_mime = upstream_message.split("Unsupported file type:", 1)[-1].strip()
                hint = f"不支持的文件类型: {bad_mime}。请转换为 PDF、图片或纯文本后再上传。"
                raise HTTPException(400, hint)

            model_armor = _extract_model_armor_violation(payload)
            if model_armor:
                # 标记为不可重试，避免上层继续切换账号/重建 session（无意义且更难定位）。
                raise HTTPException(
                    400,
                    {
                        "error": {
                            "message": "Upload blocked by Google Model Armor (unsafe content).",
                            "type": "model_armor_violation",
                            "code": 400,
                            "upstream": {
                                "service": "biz-discoveryengine.googleapis.com",
                                "endpoint": "widgetAddContextFile",
                                "status": "INVALID_ARGUMENT",
                                "code": upstream_code,
                                "message": upstream_message,
                                **model_armor,
                            },
                            "retriable": False,
                            "hint": "文件/上下文被判定包含不安全内容（提示注入/越狱等）。请清理文件内容或改为仅上传安全摘录。",
                        }
                    },
                )

        # 默认透传（保留原始响应体，方便排查）
        raise HTTPException(
            r.status_code,
            {
                "error": {
                    "message": f"Upload failed: {error_text}",
                    "type": "upstream_error",
                    "code": r.status_code,
                    "upstream": {
                        "service": "biz-discoveryengine.googleapis.com",
                        "endpoint": "widgetAddContextFile",
                    },
                    "retriable": r.status_code >= 500,
                }
            },
        )

    data = r.json()
    file_id = data.get("addContextFileResponse", {}).get("fileId")
    logger.info(f"[FILE] [{account_manager.config.account_id}] {req_tag}文件上传成功: {mime_type}")
    return file_id


async def get_session_file_metadata(
    account_mgr: "AccountManager",
    session_name: str,
    http_client: httpx.AsyncClient,
    user_agent: str,
    request_id: str = ""
) -> dict:
    """获取session中的文件元数据，包括正确的session路径"""
    body = {
        "configId": account_mgr.config.config_id,
        "additionalParams": {"token": "-"},
        "listSessionFileMetadataRequest": {
            "name": session_name,
            "filter": "file_origin_type = AI_GENERATED"
        }
    }

    resp = await make_request_with_jwt_retry(
        account_mgr,
        "POST",
        f"{GEMINI_API_BASE}/locations/global/widgetListSessionFileMetadata",
        http_client,
        user_agent,
        request_id,
        json=body
    )

    if resp.status_code != 200:
        logger.warning(f"[IMAGE] [{account_mgr.config.account_id}] [req_{request_id}] 获取文件元数据失败: {resp.status_code}")
        return {}

    data = resp.json()
    result = {}
    file_metadata_list = data.get("listSessionFileMetadataResponse", {}).get("fileMetadata", [])

    for fm in file_metadata_list:
        fid = fm.get("fileId")
        if fid:
            result[fid] = fm

    return result


def build_image_download_url(session_name: str, file_id: str) -> str:
    """构造图片下载URL"""
    return f"{GEMINI_API_BASE}/{session_name}:downloadFile?fileId={file_id}&alt=media"


async def download_image_with_jwt(
    account_mgr: "AccountManager",
    session_name: str,
    file_id: str,
    http_client: httpx.AsyncClient,
    user_agent: str,
    request_id: str = "",
    max_retries: int = 3
) -> bytes:
    """
    使用JWT认证下载图片（带超时和重试机制）

    Args:
        account_mgr: 账户管理器
        session_name: Session名称
        file_id: 文件ID
        http_client: httpx客户端
        user_agent: User-Agent字符串
        request_id: 请求ID
        max_retries: 最大重试次数（默认3次）

    Returns:
        图片字节数据

    Raises:
        HTTPException: 下载失败
        asyncio.TimeoutError: 超时
    """
    url = build_image_download_url(session_name, file_id)
    logger.info(f"[IMAGE] [{account_mgr.config.account_id}] [req_{request_id}] 开始下载图片: {file_id[:8]}...")

    for attempt in range(max_retries):
        try:
            # 3分钟超时（180秒）- 使用 wait_for 兼容 Python 3.10
            resp = await asyncio.wait_for(
                make_request_with_jwt_retry(
                    account_mgr,
                    "GET",
                    url,
                    http_client,
                    user_agent,
                    request_id,
                    follow_redirects=True
                ),
                timeout=180
            )

            resp.raise_for_status()
            logger.info(f"[IMAGE] [{account_mgr.config.account_id}] [req_{request_id}] 图片下载成功: {file_id[:8]}... ({len(resp.content)} bytes)")
            return resp.content

        except asyncio.TimeoutError:
            logger.warning(f"[IMAGE] [{account_mgr.config.account_id}] [req_{request_id}] 图片下载超时 (尝试 {attempt + 1}/{max_retries}): {file_id[:8]}...")
            if attempt == max_retries - 1:
                raise HTTPException(504, f"Image download timeout after {max_retries} attempts")
            await asyncio.sleep(2 ** attempt)  # 指数退避：2s, 4s, 8s

        except httpx.HTTPError as e:
            logger.warning(f"[IMAGE] [{account_mgr.config.account_id}] [req_{request_id}] 图片下载失败 (尝试 {attempt + 1}/{max_retries}): {type(e).__name__}")
            if attempt == max_retries - 1:
                raise HTTPException(500, f"Image download failed: {str(e)[:100]}")
            await asyncio.sleep(2 ** attempt)  # 指数退避

        except Exception as e:
            logger.error(f"[IMAGE] [{account_mgr.config.account_id}] [req_{request_id}] 图片下载异常: {type(e).__name__}: {str(e)[:100]}")
            raise

    # 不应该到达这里
    raise HTTPException(500, "Image download failed unexpectedly")


def save_image_to_hf(
    image_data: bytes,
    chat_id: str,
    file_id: str,
    mime_type: str,
    image_dir: str,
    base_url: str,
    url_path: str = "images",
) -> str:
    """保存媒体到持久化存储,返回完整的公开URL"""
    ext_map = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
    }
    ext = ext_map.get(mime_type, ".bin")

    filename = f"{chat_id}_{file_id}{ext}"
    save_path = os.path.join(image_dir, filename)

    with open(save_path, "wb") as f:
        f.write(image_data)

    return f"{base_url}/{url_path}/{filename}"
