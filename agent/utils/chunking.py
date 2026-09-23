"""记忆写入和维护脚本共用的分块、关键词与日期提取。"""

import asyncio
import json
import re
from datetime import date

from agent.utils.text import strip_code_fence
from utils.daily_logger import get_logger

logger = get_logger("memory.chunking")

_SENT_SPLIT_RE = re.compile(r"[。！？；\n]")
_CHUNK_MAX_CHARS = 200
_CHUNK_MIN_CHARS = 10


def split_memory_text(text: str, max_chars: int = _CHUNK_MAX_CHARS,
                      min_chars: int = _CHUNK_MIN_CHARS) -> list[str]:
    """按中文标点断句 + 超长句截断 + 过短句合并。"""
    parts = _SENT_SPLIT_RE.split(text)
    chunks: list[str] = []
    buffer = ""

    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) >= max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            start = 0
            while start < len(part):
                end = min(start + max_chars, len(part))
                chunks.append(part[start:end])
                start = end
        else:
            candidate = (buffer + "。" + part) if buffer else part
            if len(candidate) <= max_chars:
                buffer = candidate
            else:
                if buffer and len(buffer) >= min_chars:
                    chunks.append(buffer)
                buffer = part

    if buffer and len(buffer) >= min_chars:
        chunks.append(buffer)
    if not chunks:
        chunks = [text]
    return chunks


def extract_keywords(text: str) -> str:
    """简易正则提取关键词。"""
    text = re.sub(r"情绪[：:][^。\n]+", "", text)
    text = re.sub(r"关系变化[：:][^。\n]+", "", text)
    text = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日?", "", text)
    text = re.sub(r"上午|下午|晚上|傍晚|中午|凌晨|早晨|早上", "", text)
    parts = re.split(r"[，,。！？；;、\s]+", text)
    keywords = [part.strip() for part in parts if len(part.strip()) >= 2 and not part.strip().isdigit()]
    return ", ".join(keywords[:8])


def extract_event_date(text: str) -> str | None:
    """从记忆文本提取日期 yyyy-mm-dd。"""
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            pass
    match = re.search(r"(\d{4})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{1,2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            pass
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), 1).isoformat()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# LLM 语义分块（主） + 函数切割（回退）
# ---------------------------------------------------------------------------

_LLM_CHUNK_TIMEOUT = 60      # LLM 分块调用超时（秒）
_LLM_CHUNK_MAX_RETRIES = 2   # LLM 调用最大重试次数


async def _chunk_with_llm(memory_text: str) -> dict | None:
    """调用 LLM 对记忆做语义分块（同时提取 keywords / event_date）。

    成功返回 {"chunks": [...], "keywords": str|None, "event_date": str|None}，
    失败返回 None（由调用方回退到函数切割）。
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from agent.utils.models import get_node_model
    from agent.prompts.tools.chunking import get_chunk_prompt

    llm = get_node_model("chunking")
    prompt = get_chunk_prompt()

    for attempt in range(_LLM_CHUNK_MAX_RETRIES + 1):
        try:
            response = await asyncio.wait_for(
                llm.ainvoke([
                    SystemMessage(content=prompt),
                    HumanMessage(content=memory_text),
                ]),
                timeout=_LLM_CHUNK_TIMEOUT,
            )
            content = (getattr(response, "content", "") or "").strip()

            # 清洗可能的 markdown 包裹
            content = strip_code_fence(content)

            parsed = json.loads(content)
            if (not isinstance(parsed, dict)
                    or not isinstance(parsed.get("chunks"), list)
                    or not parsed["chunks"]):
                raise ValueError("LLM 返回缺少有效的 'chunks' 数组")

            # 超长 chunk 再做一次函数截断
            final_chunks: list[str] = []
            for chunk in parsed["chunks"]:
                chunk = str(chunk).strip()
                if not chunk:
                    continue
                if len(chunk) > _CHUNK_MAX_CHARS:
                    final_chunks.extend(split_memory_text(chunk))
                else:
                    final_chunks.append(chunk)
            if not final_chunks:
                raise ValueError("LLM 分块结果为空")

            # 校验 event_date 格式（yyyy-mm-dd）
            event_date = parsed.get("event_date")
            if event_date:
                try:
                    event_date = date.fromisoformat(str(event_date)).isoformat()
                except ValueError:
                    event_date = None

            return {
                "chunks": final_chunks,
                "keywords": parsed.get("keywords") or None,
                "event_date": event_date,
            }

        except asyncio.TimeoutError:
            logger.warning(f"LLM 分块第 {attempt + 1} 次尝试超时")
        except Exception as e:
            logger.warning(f"LLM 分块第 {attempt + 1} 次失败: "
                           f"{type(e).__name__}: {e}")

        if attempt < _LLM_CHUNK_MAX_RETRIES:
            await asyncio.sleep(1.0)

    return None


async def chunk_memory(memory_text: str) -> tuple[list[str], str | None, str | None]:
    """切块入口：LLM 优先，无法调用时回退到函数切割。

    返回 (chunks, keywords, event_date)；后两者为自动提取结果，
    仅在调用方未显式传入时使用。
    """
    parsed = await _chunk_with_llm(memory_text)
    if parsed is not None:
        return parsed["chunks"], parsed["keywords"], parsed["event_date"]
    return (split_memory_text(memory_text),
            extract_keywords(memory_text),
            extract_event_date(memory_text))
