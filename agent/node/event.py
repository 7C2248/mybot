"""事件完成判断与记忆处理路由。"""

from langchain_core.messages import AIMessage

from agent.classes.state import AgentState
from agent.utils.context import (
    estimate_message_tokens as _estimate_message_tokens,
    estimate_tokens_from_bytes as _estimate_tokens_from_bytes,
)
from config.config import MAXIMUM_ITERATIONS, MEMORY_TOKEN_THRESHOLD, MINIMUM_ITERATIONS
from utils.daily_logger import get_logger

__all__ = ['event_judge']

logger = get_logger("node.event")


def _context_tokens(messages: list) -> int:
    """当前上下文 token 数：优先用最近 AI 消息的 input_tokens，否则按字节估算全部消息。"""
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        usage = getattr(message, "usage_metadata", None)
        if usage is None:
            continue
        if isinstance(usage, dict):
            return int(usage.get("input_tokens") or usage.get("total_tokens") or 0)
        return int(getattr(usage, "input_tokens", None)
                   or getattr(usage, "total_tokens", None) or 0)
    return sum(_estimate_message_tokens(message) for message in messages)


def event_judge(state: AgentState) -> bool:
    """事件判断路由：返回是否进入记忆处理。

    轮数下限前不处理，上限后强制处理；区间内按最近模型调用的输入
    token 用量判断上下文压力，达到阈值直接处理。模型不返回用量
    信息时按消息字节数估算，不再调用语义事件判断模型。
    """
    if not state.get('memory_storage_enabled', True):
        return False
    if state.get("memory_pending_job") or state.get("memory_active_job"):
        return False
    if state.get("need_event_judge", False):
        iteration = state.get("iteration", 0)
        if MINIMUM_ITERATIONS <= iteration <= MAXIMUM_ITERATIONS:
            messages = state.get("messages", [])
            context_tokens = _context_tokens(messages)
            if context_tokens >= MEMORY_TOKEN_THRESHOLD:
                logger.info("上下文 token 达到阈值，进入记忆处理 | tokens=%d | threshold=%d",
                            context_tokens, MEMORY_TOKEN_THRESHOLD)
                return True
        elif iteration > MAXIMUM_ITERATIONS:
            return True

    return False
