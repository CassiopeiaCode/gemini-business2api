"""Gemini 原生 API 格式支持模块

本模块提供 Gemini 原生 API 格式的数据模型定义，包括：
- 请求格式：GeminiPart, GeminiContent, GeminiGenerationConfig, GeminiRequest
- 响应格式：流式和非流式响应转换
- 错误格式：Gemini 格式错误响应

用于将 Gemini 原生格式请求转换为内部格式，以及将内部响应转换为 Gemini 原生格式。
"""

from __future__ import annotations

import base64
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
from pydantic import BaseModel


# ==================== 请求数据模型 ====================


class GeminiPart(BaseModel):
    """Gemini 消息部分

    表示消息中的一个内容部分，可以是文本或内联数据（如图片）。

    Attributes:
        text: 文本内容
        inlineData: 内联数据，格式为 {"mimeType": str, "data": str}，
                   其中 data 为 base64 编码的数据
    """

    text: Optional[str] = None
    inlineData: Optional[Dict[str, str]] = None  # {"mimeType": str, "data": str}


class GeminiContent(BaseModel):
    """Gemini 消息内容

    表示一条完整的消息，包含角色和内容部分列表。

    Attributes:
        role: 消息角色，"user" 表示用户，"model" 表示模型/助手
        parts: 消息内容部分列表
    """

    role: str  # "user" 或 "model"
    parts: List[GeminiPart]


class GeminiThinkingConfig(BaseModel):
    """思考配置

    控制模型的思考行为，用于启用/配置思考模式。

    Attributes:
        thinkingLevel: 思考级别，可选值 "high", "medium", "low"
        includeThoughts: 是否在响应中包含思考过程
    """

    thinkingLevel: Optional[str] = None  # "high", "medium", "low"
    includeThoughts: Optional[bool] = None


class GeminiImageConfig(BaseModel):
    """图片生成配置

    控制图片生成的参数。

    Attributes:
        aspectRatio: 图片宽高比，如 "16:9", "4:3", "1:1"
        imageSize: 图片尺寸，如 "1K", "2K"
    """

    aspectRatio: Optional[str] = None
    imageSize: Optional[str] = None


class GeminiGenerationConfig(BaseModel):
    """生成配置

    控制模型生成行为的配置参数。

    Attributes:
        temperature: 温度参数，控制输出的随机性，范围 0-2
        thinkingConfig: 思考配置
        responseModalities: 响应模态列表，如 ["TEXT", "IMAGE"]
        imageConfig: 图片生成配置
    """

    temperature: Optional[float] = None
    thinkingConfig: Optional[GeminiThinkingConfig] = None
    responseModalities: Optional[List[str]] = None  # ["TEXT", "IMAGE"]
    imageConfig: Optional[GeminiImageConfig] = None


class GeminiRequest(BaseModel):
    """Gemini 原生格式请求

    Gemini API 的请求体格式，包含对话内容、生成配置和系统指令。

    Attributes:
        contents: 对话内容列表，包含用户和模型的消息
        generationConfig: 生成配置，控制模型行为
        systemInstruction: 系统指令，用于设置模型的行为准则
    """

    contents: List[GeminiContent]
    generationConfig: Optional[GeminiGenerationConfig] = None
    systemInstruction: Optional[GeminiContent] = None


# ==================== 请求格式转换器 ====================


class GeminiRequestConverter:
    """Gemini 请求格式转换器

    负责将 Gemini 原生格式请求转换为内部格式（兼容 OpenAI ChatRequest）。
    """

    @staticmethod
    def to_internal_format(gemini_request: GeminiRequest, model: str) -> Dict[str, Any]:
        messages: List[Dict[str, Any]] = []

        # 处理系统指令 -> system message
        if gemini_request.systemInstruction:
            system_text = GeminiRequestConverter._extract_text(gemini_request.systemInstruction.parts)
            if system_text:
                messages.append({"role": "system", "content": system_text})

        # 处理 contents
        for content in gemini_request.contents:
            role = "assistant" if content.role == "model" else "user"

            has_inline_data = any(p.inlineData for p in content.parts)
            if has_inline_data:
                content_parts: List[Dict[str, Any]] = []
                for part in content.parts:
                    if part.text:
                        content_parts.append({"type": "text", "text": part.text})
                    if part.inlineData:
                        mime = part.inlineData.get("mimeType", "image/png")
                        data = part.inlineData.get("data", "")
                        content_parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{data}"},
                            }
                        )
                messages.append({"role": role, "content": content_parts})
            else:
                text = GeminiRequestConverter._extract_text(content.parts)
                messages.append({"role": role, "content": text})

        result: Dict[str, Any] = {"model": model, "messages": messages, "stream": True}

        if gemini_request.generationConfig and gemini_request.generationConfig.temperature is not None:
            result["temperature"] = gemini_request.generationConfig.temperature

        return result

    @staticmethod
    def _extract_text(parts: List[GeminiPart]) -> str:
        return "".join(p.text or "" for p in parts)


# ==================== 图片解析辅助函数 ====================


def parse_markdown_image(text: str) -> Tuple[str, list]:
    """解析文本中的 Markdown 图片，提取图片数据

    支持两种格式：
    1. Base64 格式：![alt](data:image/png;base64,xxx)
    2. URL 格式：![alt](https://xxx/images/xxx.png)
    """
    images: list = []

    pattern = r"!\[([^\]]*)\]\(([^)]+)\)"

    def extract_image(match: re.Match) -> str:
        url = match.group(2)

        if url.startswith("data:"):
            data_match = re.match(r"data:([^;]+);base64,(.+)", url)
            if data_match:
                mime_type = data_match.group(1)
                base64_data = data_match.group(2)
                images.append({"mimeType": mime_type, "data": base64_data})
        else:
            images.append({"mimeType": "image/png", "data": None, "url": url})

        return ""

    clean_text = re.sub(pattern, extract_image, text)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text).strip()
    return clean_text, images


async def download_image_as_base64(url: str, timeout: float = 30.0) -> Tuple[str, str]:
    async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
        response = await client.get(url)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "image/png")
        mime_type = content_type.split(";")[0].strip()

        base64_data = base64.b64encode(response.content).decode("utf-8")
        return mime_type, base64_data


# ==================== 响应格式转换器 ====================


class GeminiResponseConverter:
    """Gemini 响应格式转换器"""

    def __init__(self, model_version: str):
        self.model_version = model_version
        self.response_id = str(uuid.uuid4()).replace("-", "")[:24]
        self.prompt_token_count = 0
        self.candidates_token_count = 0
        self.thoughts_token_count = 0

    def create_stream_chunk(
        self,
        text: Optional[str] = None,
        is_thought: bool = False,
        inline_data: Optional[Dict[str, str]] = None,
        finish_reason: Optional[str] = None,
        thought_signature: Optional[str] = None,
    ) -> Dict[str, Any]:
        parts: List[Dict[str, Any]] = []

        if thought_signature is not None and text == "" and not inline_data:
            parts.append({"text": "", "thoughtSignature": thought_signature})
        else:
            if text is not None:
                part: Dict[str, Any] = {"text": text}
                if is_thought:
                    part["thought"] = True
                parts.append(part)

        if inline_data:
            part2: Dict[str, Any] = {"inlineData": inline_data}
            if is_thought:
                part2["thought"] = True
            elif thought_signature is not None:
                part2["thoughtSignature"] = thought_signature
            parts.append(part2)

        candidate: Dict[str, Any] = {
            "content": {"parts": parts, "role": "model"},
            "index": 0,
        }
        if finish_reason:
            candidate["finishReason"] = finish_reason

        if text:
            token_estimate = len(text) // 4 + 1
            if is_thought:
                self.thoughts_token_count += token_estimate
            else:
                self.candidates_token_count += token_estimate

        return {
            "candidates": [candidate],
            "usageMetadata": self._build_usage_metadata(),
            "modelVersion": self.model_version,
            "responseId": self.response_id,
        }

    def create_non_stream_response(self, content_parts: List[Dict[str, Any]], finish_reason: str = "STOP") -> Dict[str, Any]:
        for part in content_parts:
            if "text" in part and part["text"]:
                token_estimate = len(part["text"]) // 4 + 1
                if part.get("thought"):
                    self.thoughts_token_count += token_estimate
                else:
                    self.candidates_token_count += token_estimate

        return {
            "candidates": [
                {
                    "content": {"parts": content_parts, "role": "model"},
                    "finishReason": finish_reason,
                    "index": 0,
                }
            ],
            "usageMetadata": self._build_usage_metadata(),
            "modelVersion": self.model_version,
            "responseId": self.response_id,
        }

    def _build_usage_metadata(self) -> Dict[str, Any]:
        total = self.prompt_token_count + self.candidates_token_count + self.thoughts_token_count

        metadata: Dict[str, Any] = {
            "promptTokenCount": self.prompt_token_count,
            "totalTokenCount": total,
            "promptTokensDetails": [
                {"modality": "TEXT", "tokenCount": self.prompt_token_count},
            ],
        }

        if self.candidates_token_count > 0:
            metadata["candidatesTokenCount"] = self.candidates_token_count
        if self.thoughts_token_count > 0:
            metadata["thoughtsTokenCount"] = self.thoughts_token_count

        return metadata

    def set_prompt_tokens(self, count: int) -> None:
        self.prompt_token_count = count

    def set_candidates_tokens(self, count: int) -> None:
        self.candidates_token_count = count

    def set_thoughts_tokens(self, count: int) -> None:
        self.thoughts_token_count = count


# ==================== 错误格式转换器 ====================


class GeminiErrorConverter:
    """Gemini 错误格式转换器"""

    STATUS_MAP: Dict[int, str] = {
        400: "INVALID_ARGUMENT",
        401: "UNAUTHENTICATED",
        403: "PERMISSION_DENIED",
        404: "NOT_FOUND",
        429: "RESOURCE_EXHAUSTED",
        500: "INTERNAL",
        503: "UNAVAILABLE",
        504: "DEADLINE_EXCEEDED",
    }

    @staticmethod
    def create_error_response(status_code: int, message: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        status = GeminiErrorConverter.STATUS_MAP.get(status_code, "UNKNOWN")
        error: Dict[str, Any] = {"error": {"code": status_code, "message": message, "status": status}}
        if details:
            error["error"]["details"] = details
        return error

    @staticmethod
    def get_status_for_code(status_code: int) -> str:
        return GeminiErrorConverter.STATUS_MAP.get(status_code, "UNKNOWN")