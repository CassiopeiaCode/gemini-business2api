import re
from typing import Optional


def extract_verification_code(text: str) -> Optional[str]:
    """提取验证码"""
    if not text:
        return None

    # 预处理：移除 HTML 标签和常见的 HTML 关键词
    # 移除完整的 HTML 标签
    text_cleaned = re.sub(r'<[^>]+>', ' ', text)
    # 移除 DOCTYPE 声明
    text_cleaned = re.sub(r'<!DOCTYPE[^>]*>', ' ', text_cleaned, flags=re.IGNORECASE)
    # 移除常见的 HTML 实体
    text_cleaned = re.sub(r'&[a-z]+;', ' ', text_cleaned, flags=re.IGNORECASE)

    # 策略1: 上下文关键词匹配（中英文冒号）- 优先级最高
    context_pattern = r"(?:验证码|code|verification|passcode|pin).*?[:：]\s*([A-Za-z0-9]{4,8})\b"
    match = re.search(context_pattern, text_cleaned, re.IGNORECASE)
    if match:
        candidate = match.group(1)
        # 排除 CSS 单位值
        if not re.match(r"^\d+(?:px|pt|em|rem|vh|vw|%)$", candidate, re.IGNORECASE):
            return candidate

    # 策略2: 独立的6位字母数字混合（周围有空白或换行符）
    # 使用单词边界确保不会匹配到 HTML 标签或其他文本的一部分
    match = re.search(r'\b[A-Z0-9]{6}\b', text_cleaned)
    if match:
        code = match.group(0)
        # 额外验证：至少包含一个字母和一个数字（排除纯字母如 DOCTYP）
        if re.search(r'[A-Z]', code) and re.search(r'[0-9]', code):
            return code

    # 策略3: 6位数字（降级为备选）
    digits = re.findall(r"\b\d{6}\b", text_cleaned)
    if digits:
        return digits[0]

    return None
