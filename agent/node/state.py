"""轮初状态重置、重试输入去重与迭代计数节点。"""

import re
import uuid

from langchain_core.messages import HumanMessage, RemoveMessage
from agent.classes.state import AgentState

from utils.daily_logger import get_logger

__all__ = ['begin_turn', 'increment_iteration']

logger = get_logger("node.state")


def increment_iteration(state: AgentState):
    """更新成功对话轮数"""
    iteration = state.get("iteration", 0) + 1
    result = {"iteration": iteration}
    if state.get("service_run_id"):
        result["service_reply_counted"] = True
    return result


def _retry_input_content(message: HumanMessage):
    """CLI 每次提交会添加时间戳，比较重试输入时只忽略开头的时间戳。"""
    content = message.content
    if isinstance(content, str):
        return re.sub(r"\A<timestamp>[^<]*</timestamp>\r?\n?", "", content, count=1)
    return content


def begin_turn(state: AgentState):
    """重置本轮状态；失败重试复用原用户输入，成功历史及工具消息保持原样。"""
    result = {
        "turn_id": state.get("service_run_id") or uuid.uuid4().hex, "draft_reply": "", "draft_status": "pending",
        "draft_reasoning": "", "draft_usage": None,
        "service_reply_counted": False,
        "check_status": "pending", "check_issues": [], "check_reply": "",
        "check_feedback": "", "check_rounds": 0, "reply_error": "",
        "retry_message_id": "",
    }
    retry_id = state.get("retry_message_id")
    if state.get("reply_error") and retry_id:
        messages = state.get("messages", [])
        retry_index = next((index for index, message in enumerate(messages)
                            if message.id == retry_id and isinstance(message, HumanMessage)), None)
        if retry_index is not None:
            repeated = messages[retry_index + 1:]
            original = _retry_input_content(messages[retry_index])
            # 仅合并失败输入紧随其后的重复提交，不按内容全局去重。
            if repeated and all(isinstance(message, HumanMessage)
                                and _retry_input_content(message) == original for message in repeated):
                result["messages"] = [RemoveMessage(id=message.id) for message in repeated]
                logger.info("重试复用用户输入 | user_message_id=%s | 删除重复消息数=%d",
                            retry_id, len(repeated))
    return result
