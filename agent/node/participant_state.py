"""用一次模型调用共同更新角色和用户状态，在输入和正式回复后对称执行。"""

import asyncio
import json

from langchain_core.messages import AIMessage, HumanMessage

from agent.classes.participant_state import ParticipantStateUpdate
from agent.utils.models import get_node_model
from agent.classes.state import AgentState
from agent.utils.state import prepare_character_state, prepare_user_state, prepare_world_state
from agent.prompts import get_prompt
from agent.utils.text import content_text, strip_code_fence, strip_timestamps
from utils.daily_logger import get_logger

__all__ = ['create_participant_state_node']

logger = get_logger("node.participant_state")
_STATE_TIMEOUT = 90
_STATE_MAX_RETRIES = 2


def _message_pair(messages: list, trigger: str) -> list[dict]:
    """取最新消息及其之前最近的另一方消息；首轮允许只有用户消息。"""
    latest_type = HumanMessage if trigger == "user" else AIMessage
    pair = []
    for message in reversed(messages):
        if not isinstance(message, (HumanMessage, AIMessage)):
            continue
        if isinstance(message, AIMessage) and (message.tool_calls or message.invalid_tool_calls):
            continue
        if not pair:
            # 不在没有新用户输入/正式回复时重新处理旧消息。
            if not isinstance(message, latest_type):
                return []
        elif isinstance(message, latest_type):
            continue
        text = strip_timestamps(content_text(message.content)).strip()
        if not text:
            if not pair:
                return []
            continue
        pair.append({
            "speaker": "user" if isinstance(message, HumanMessage) else "character",
            "is_new": not pair,
            "content": text,
        })
        if len(pair) == 2:
            break
    return list(reversed(pair))


def create_participant_state_node(trigger: str = "user"):
    if trigger not in ("user", "reply"):
        raise ValueError(f"[ParticipantState] 未知 trigger: {trigger}")
    llm = get_node_model("participant_state")

    async def node(state: AgentState):
        pair = _message_pair(state.get("messages", []), trigger)
        if not pair:
            return {}
        character, character_text = prepare_character_state(state.get("character_state"))
        user, user_text = prepare_user_state(state.get("user_state"))
        _, world_text = prepare_world_state(state.get("world_state"))
        evidence = json.dumps(pair, ensure_ascii=False, indent=2)
        messages = [
            {"role": "system", "content": get_prompt("participant_state")},
            {"role": "user", "content": (
                character_text + user_text + world_text
                + f"\n<update_trigger>{trigger}</update_trigger>\n"
                + "<evidence>\n" + evidence + "\n</evidence>"
            )},
        ]

        for attempt in range(_STATE_MAX_RETRIES + 1):
            try:
                response = await asyncio.wait_for(llm.ainvoke(messages), timeout=_STATE_TIMEOUT)
                content = strip_timestamps(content_text(response.content)).strip()
                content = strip_code_fence(content)

                update = ParticipantStateUpdate.model_validate_json(content)
                # 缺失字段保留；显式 null 可清除已失效且无法确定的新状态。
                # 两份状态全部验证成功后再一起返回，避免只写回一方。
                result = {
                    "character_state": {**character, **update.character_state.model_dump(exclude_unset=True)},
                    "user_state": {**user, **update.user_state.model_dump(exclude_unset=True)},
                }
                logger.info("participant_state 更新 (%s): %s", trigger, result)
                return result
            except asyncio.TimeoutError:
                logger.warning("participant_state (%s) 第 %d 次尝试超时", trigger, attempt + 1)
            except Exception as exc:
                logger.warning("participant_state (%s) 第 %d 次失败: %s: %s",
                               trigger, attempt + 1, type(exc).__name__, exc)
            if attempt < _STATE_MAX_RETRIES:
                await asyncio.sleep(1.0)
        return {}

    return node
