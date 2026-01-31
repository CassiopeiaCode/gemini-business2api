"""消息处理模块

负责消息的解析、文本提取和会话指纹生成
"""
import asyncio
import base64
import hashlib
import logging
import re
from typing import List, TYPE_CHECKING, Tuple

import httpx

if TYPE_CHECKING:
    from main import Message

logger = logging.getLogger(__name__)


def _normalize_message_text(content) -> str:
    if isinstance(content, list):
        # 多模态消息：只提取文本部分
        text = extract_text_from_content(content)
    else:
        text = str(content)
    return text.strip().lower()


def _hash_key(parts: List[str], client_identifier: str = "") -> str:
    prefix = "|".join(parts)
    if client_identifier:
        prefix = f"{client_identifier}|{prefix}"
    return hashlib.md5(prefix.encode()).hexdigest()


def _truncate_messages_to_nth_user(messages: List[dict], user_index_1based: int) -> List[dict]:
    """
    返回从开头截断到“第 user_index_1based 条 user 消息（含）”为止的消息列表。
    若找不到对应 user 消息，则返回原 messages（保守行为）。
    """
    if user_index_1based <= 0:
        return []
    user_seen = 0
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            user_seen += 1
            if user_seen == user_index_1based:
                return messages[: i + 1]
    return messages


def get_conversation_keys(messages: List[dict], client_identifier: str = "") -> Tuple[str, str, bool]:
    """
    生成会话复用相关的 key（lookup_key / store_key）以及是否强制新会话。

    规则（按 user 消息条数统计）：
    - user_count < 2: 强制新会话；store_key=“最后一条 user 及之前所有消息”；lookup_key 同 store_key
    - user_count == 2: 强制新会话；store_key=“最后一条 user 及之前所有消息”；lookup_key 同 store_key（但调用方需跳过查找）
    - user_count >= 3: lookup_key=“倒数第二条 user 及之前所有消息”；store_key=“最后一条 user 及之前所有消息”
    """
    if not messages:
        empty = f"{client_identifier}:empty" if client_identifier else "empty"
        return empty, empty, True

    user_count = sum(1 for m in messages if m.get("role") == "user")

    # store_key: 截断到最后一条 user（通常就是全量 messages，保守处理）
    store_msgs = _truncate_messages_to_nth_user(messages, user_count if user_count > 0 else 0)
    store_parts: List[str] = []
    for msg in store_msgs:
        role = msg.get("role", "")
        text = _normalize_message_text(msg.get("content", ""))
        store_parts.append(f"{role}:{text}")
    store_key = _hash_key(store_parts, client_identifier=client_identifier)

    if user_count < 2:
        return store_key, store_key, True

    if user_count == 2:
        # 不复用，但会保存 store_key 以便未来复用
        return store_key, store_key, True

    # user_count >= 3
    lookup_msgs = _truncate_messages_to_nth_user(messages, user_count - 1)
    lookup_parts: List[str] = []
    for msg in lookup_msgs:
        role = msg.get("role", "")
        text = _normalize_message_text(msg.get("content", ""))
        lookup_parts.append(f"{role}:{text}")
    lookup_key = _hash_key(lookup_parts, client_identifier=client_identifier)

    return lookup_key, store_key, False


def get_conversation_key(messages: List[dict], client_identifier: str = "") -> str:
    """
    兼容入口：返回 lookup_key。
    调用方如需“强制新会话但仍保存映射”的能力，请使用 [`get_conversation_keys()`](core/message.py:1)。
    """
    lookup_key, _store_key, _force_new = get_conversation_keys(messages, client_identifier=client_identifier)
    return lookup_key


def extract_text_from_content(content) -> str:
    """
    从消息 content 中提取文本内容
    统一处理字符串和多模态数组格式
    """
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        # 多模态消息：只提取文本部分
        return "".join([x.get("text", "") for x in content if x.get("type") == "text"])
    else:
        return str(content)


async def parse_last_message(messages: List['Message'], http_client: httpx.AsyncClient, request_id: str = ""):
    """解析最后一条消息，分离文本和文件（支持图片、PDF、文档等，base64 和 URL）"""
    if not messages:
        return "", []

    last_msg = messages[-1]
    content = last_msg.content

    text_content = ""
    images = [] # List of {"mime": str, "data": str_base64} - 兼容变量名，实际支持所有文件
    image_urls = []  # 需要下载的 URL - 兼容变量名，实际支持所有文件

    if isinstance(content, str):
        text_content = content
    elif isinstance(content, list):
        for part in content:
            if part.get("type") == "text":
                text_content += part.get("text", "")
            elif part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                # 解析 Data URI: data:mime/type;base64,xxxxxx (支持所有 MIME 类型)
                match = re.match(r"data:([^;]+);base64,(.+)", url)
                if match:
                    images.append({"mime": match.group(1), "data": match.group(2)})
                elif url.startswith(("http://", "https://")):
                    image_urls.append(url)
                else:
                    logger.warning(f"[FILE] [req_{request_id}] 不支持的文件格式: {url[:30]}...")

    # 并行下载所有 URL 文件（支持图片、PDF、文档等）
    if image_urls:
        async def download_url(url: str):
            try:
                resp = await http_client.get(url, timeout=30, follow_redirects=True)
                if resp.status_code == 404:
                    logger.warning(f"[FILE] [req_{request_id}] URL文件已失效(404)，已跳过: {url[:50]}...")
                    return None
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "application/octet-stream").split(";")[0]
                # 移除图片类型限制，支持所有文件类型
                b64 = base64.b64encode(resp.content).decode()
                logger.info(f"[FILE] [req_{request_id}] URL文件下载成功: {url[:50]}... ({len(resp.content)} bytes, {content_type})")
                return {"mime": content_type, "data": b64}
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code if e.response else "unknown"
                logger.warning(f"[FILE] [req_{request_id}] URL文件下载失败({status_code}): {url[:50]}... - {e}")
                return None
            except Exception as e:
                logger.warning(f"[FILE] [req_{request_id}] URL文件下载失败: {url[:50]}... - {e}")
                return None

        results = await asyncio.gather(*[download_url(u) for u in image_urls], return_exceptions=True)
        safe_results = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"[FILE] [req_{request_id}] URL文件下载异常: {type(result).__name__}: {str(result)[:120]}")
                continue
            safe_results.append(result)
        images.extend([r for r in safe_results if r])

    return text_content, images


def build_full_context_text(messages: List['Message']) -> str:
    """仅拼接历史文本，图片只处理当次请求的"""
    prompt = ""
    for msg in messages:
        role = "User" if msg.role in ["user", "system"] else "Assistant"
        content_str = extract_text_from_content(msg.content)

        # 为多模态消息添加图片标记
        if isinstance(msg.content, list):
            image_count = sum(1 for part in msg.content if part.get("type") == "image_url")
            if image_count > 0:
                content_str += "[图片]" * image_count

        prompt += f"{role}: {content_str}\n\n"
    return prompt
