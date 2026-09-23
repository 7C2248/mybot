"""模型响应中共用的内容块、时间戳和 Markdown 容器处理。"""

import re


def content_text(content: str | list | None) -> str:
    """提取消息中的文本块，忽略图片等非文本内容。"""
    if isinstance(content, list):
        return "".join(
            block if isinstance(block, str) else block.get("text", "")
            for block in content if isinstance(block, (str, dict))
        )
    return content or ""


def strip_timestamps(text: str) -> str:
    """移除对话时间戳，保留其他正文和空白。"""
    return re.sub(r"<timestamp>.*?</timestamp>\n?", "", text, flags=re.DOTALL)


def strip_code_fence(text: str) -> str:
    """移除模型 JSON 输出外层的 Markdown 代码围栏。"""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text
