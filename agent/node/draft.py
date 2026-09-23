"""主回复节点：自行提取事实、判断感知并调用记忆工具，候选正文交给 check。"""

import asyncio
import re

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage
from langchain_core.tools import BaseTool

from agent.utils.models import get_node_model
from agent.classes.state import AgentState
from agent.utils.state import prepare_character_state, prepare_user_state, prepare_world_state
from agent.prompts import get_draft_prompt
from utils.time import get_time
from agent.utils.text import content_text, strip_timestamps
from utils.daily_logger import get_logger

__all__ = ['create_draft_node', 'draft_judge', 'commit_reply', 'reply_failed']

logger = get_logger("node.draft")
_DRAFT_MAX_RETRIES = 3


def _extract_reply(content: str) -> str:
    """提取候选正文；兼容旧版格式的思考容器，内部内容不进入正式对话。"""
    text = content or ""
    # 先移除完整内部容器，避免把预演中的 <reply> 当成最终正文。
    text = re.sub(r"<(thinking_process|monologue)>.*?</\1>", "", text, flags=re.DOTALL)
    # 未闭合的内部容器后面都属于未完成的内部输出。
    text = re.split(r"<(?:thinking_process|monologue)>", text, maxsplit=1)[0]
    reply = re.search(r"<reply>\s*(.*?)\s*</reply>", text, flags=re.DOTALL)
    return (reply.group(1) if reply else text).strip()


def _usage_dict(usage) -> dict | None:
    """把模型的 usage_metadata 转成纯 dict，供 checkpoint 持久化。"""
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump(exclude_none=True)
    return {key: value for key, value in usage.items() if value is not None}


def create_draft_node(
    character_name: str,
    language: str = "zh",
    character_profile: str | None = None,
    tools: list[BaseTool] | None = None,
):
    tools = list(tools or [])
    llm = get_node_model("main")
    if tools:
        llm = llm.bind_tools(tools, parallel_tool_calls=True)
    prompt_base = get_draft_prompt(
        character_name=character_name, language=language, character_profile=character_profile,
    )

    async def node(state: AgentState):
        # 原样保留 AI 工具调用及 ToolMessage；本轮和后续轮次都能直接读取检索原文。
        messages = state.get("messages", [])
        _, world = prepare_world_state(state.get("world_state"))
        _, character = prepare_character_state(state.get("character_state"))
        _, user = prepare_user_state(state.get("user_state"))
        system_content = prompt_base + "\n" + world + character + user
        if not any(tool.name == "memory_query" for tool in tools):
            system_content += "\n当前未提供记忆检索工具；使用已有消息中的记忆，缺少依据时保留未知，不假称已经检索。\n"
        feedback = (state.get("check_feedback") or "").strip()
        if feedback:
            if not any(issue.get("type") == "refusal" for issue in state.get("check_issues") or []):
                system_content += (
                    "\n<previous_draft>\n" + state.get("draft_reply", "") + "\n</previous_draft>\n")
            system_content += (
                "<check_feedback>\n" + feedback + "\n</check_feedback>\n"
                + "上一版候选未通过检查，请修正指出的问题，重新生成完整候选回复。\n"
            )

        reset_check = {"check_status": "pending", "check_issues": [], "check_reply": "",
                       "draft_usage": None, "draft_reasoning": ""}
        for attempt in range(_DRAFT_MAX_RETRIES + 1):
            try:
                # 不设置 draft 超时；正常等待模型或由调用方取消。
                response = await llm.ainvoke([SystemMessage(content=system_content)] + messages)
                if response.invalid_tool_calls:
                    raise ValueError("工具调用参数无法解析，不能作为角色正文")
                if response.tool_calls:
                    call_ids = [call.get("id") for call in response.tool_calls]
                    if (not tools or not all(call_ids) or len(call_ids) != len(set(call_ids))
                            or any(call.get('name') not in {tool.name for tool in tools} for call in response.tool_calls)):
                        raise ValueError("工具不可用或工具调用 ID 无效")
                    return dict(reset_check, messages=[response], draft_status="calling_tools")

                content = content_text(response.content)
                if not content.strip():
                    reasoning = response.additional_kwargs.get("reasoning_content", "") or ""
                    if "</thinking_process>" in reasoning:
                        content = reasoning.split("</thinking_process>", 1)[1]

                reply = strip_timestamps(_extract_reply(content))
                # 空正文和拒答均由独立 check 节点处理。
                logger.info("draft 生成成功 | 上下文消息数=%d", len(messages))
                return dict(reset_check, draft_reply=reply, draft_status="ready",
                            draft_reasoning=(response.additional_kwargs.get("reasoning_content") or "").strip(),
                            draft_usage=_usage_dict(getattr(response, "usage_metadata", None)))
            except Exception as exc:
                logger.warning(f"draft 第 {attempt + 1} 次失败: {type(exc).__name__}: {exc}")
                if attempt < _DRAFT_MAX_RETRIES:
                    await asyncio.sleep(1.0)
        logger.warning("draft 重试耗尽 | turn_id=%s | 最大重试次数=%d | 去向=reply_failed",
                       state.get("turn_id", ""), _DRAFT_MAX_RETRIES)
        return dict(reset_check, draft_reply="", draft_status="failed")

    return node


def draft_judge(state: AgentState) -> str:
    if state.get("draft_status") == "calling_tools":
        return "tools"
    return "check" if state.get("draft_status") == "ready" else "reply_failed"


def commit_reply(state: AgentState):
    """只提交被检查的同一份草稿，稳定 ID 避免 checkpoint 重放产生重复回复。"""
    reply = (state.get("draft_reply") or "").strip()
    if (state.get("draft_status") != "ready" or state.get("check_status") != "passed"
            or state.get("check_issues") or not reply or state.get("check_reply") != reply):
        raise ValueError("禁止提交未通过自检或检查后发生变化的草稿")
    turn_id = state.get("turn_id")
    if not turn_id:
        raise ValueError("缺少本轮 ID")
    content = f"<timestamp>{get_time()}</timestamp>\n" + reply
    reasoning = (state.get("draft_reasoning") or "").strip()
    message_kwargs = {"additional_kwargs": {"reasoning_content": reasoning}} if reasoning else {}
    return {"messages": [AIMessage(id=f"reply_{turn_id}", content=content,
                                   usage_metadata=state.get("draft_usage"), **message_kwargs)],
            "draft_reply": "", "draft_status": "committed", "check_reply": "",
            "draft_reasoning": "", "reply_error": "", "draft_usage": None}


def reply_failed(state: AgentState):
    """失败后保留最近用户输入，成组删除其后的消息，供下一次调用重试。"""
    messages = state.get("messages", [])
    last_user_index = next((index for index in range(len(messages) - 1, -1, -1)
                            if isinstance(messages[index], HumanMessage)), None)
    result = {"draft_reply": "", "draft_status": "failed", "check_reply": "",
              "check_feedback": "", "retry_message_id": "", "draft_reasoning": "",
              "draft_usage": None,
              "reply_error": "本轮回复生成或验证失败，未提交角色回复，请重试。"}
    if last_user_index is not None:
        # add_messages 按 ID 合并；必须显式删除，返回历史切片不会截断队列。
        removals = [RemoveMessage(id=message.id) for message in messages[last_user_index + 1:]]
        result.update(messages=removals, retry_message_id=messages[last_user_index].id,
                      reply_error="本轮回复生成或验证失败，已退回最近的用户输入，请重新提交该输入重试。")
        if removals:
            logger.warning("回复失败，已删除用户输入后的失败轮消息 | turn_id=%s | user_message_id=%s | 删除消息数=%d",
                           state.get("turn_id", ""), messages[last_user_index].id, len(removals))
        else:
            logger.warning("回复失败，用户输入后无残留消息，无需删除 | turn_id=%s | user_message_id=%s",
                           state.get("turn_id", ""), messages[last_user_index].id)
    else:
        logger.warning("回复失败，未找到可回退的用户输入 | turn_id=%s", state.get("turn_id", ""))
    return result
