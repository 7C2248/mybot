"""消息快照、增量处理边界和安全裁剪；不访问模型或数据库。"""

import hashlib
import json

from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage, messages_to_dict


def freeze_messages(messages: list) -> list[dict]:
    # JSON 往返隔离嵌套的 tool_calls/content；任务不持有图状态的对象引用。
    result = json.loads(json.dumps(messages_to_dict(messages), ensure_ascii=False))
    ids = [item["data"].get("id") for item in result]
    if not all(ids) or len(set(ids)) != len(ids):
        raise ValueError("记忆快照要求每条消息有唯一且稳定的 ID")
    return result


def message_fingerprint(message: dict) -> str:
    data = message["data"]
    relevant = {key: data.get(key) for key in
                ("id", "content", "name", "tool_calls", "tool_call_id")}
    return hashlib.sha256(json.dumps(
        {"type": message["type"], "data": relevant},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def build_memory_payload(state: dict, character_name: str, thread_id: str) -> dict | None:
    messages = freeze_messages(state.get("messages", []))
    if not messages:
        return None
    ids = [item["data"]["id"] for item in messages]
    processed = state.get("memory_processed_through")
    start = 0
    if processed:
        if processed not in ids:
            raise ValueError("已处理消息边界缺失；不能自动重置记忆进度")
        boundary = ids.index(processed)
        expected = state.get("memory_processed_fingerprint")
        if expected and message_fingerprint(messages[boundary]) != expected:
            raise ValueError("已处理消息发生变化；不能沿旧进度继续整理")
        start = boundary + 1
    if start == len(messages):
        return None
    payload = {
        "version": 1, "character_name": character_name, "thread_id": thread_id,
        "turn_id": state["turn_id"], "iteration": state.get("iteration", 0),
        "world_state": state.get("world_state", {}), "messages": messages,
        "new_message_start": start, "through_message_id": ids[-1],
        "base_processed_through": processed,
    }
    if 'memory_policy_version' in state:
        payload.update(memory_policy_version=state['memory_policy_version'],
                       memory_retrieval_enabled=state.get('memory_retrieval_enabled', True),
                       memory_storage_enabled=state.get('memory_storage_enabled', True))
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    payload["job_key"] = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return payload


def history_removals(messages: list, index: int | None) -> list[RemoveMessage]:
    """沿用原裁剪规则：保留完整工具组，并至少保留六条消息。"""
    if type(index) is not int or index <= 0:
        return []
    limit = min(index, max(0, len(messages) - 6))
    pending = set()
    safe_index = 0
    for position, message in enumerate(messages[:limit]):
        if isinstance(message, AIMessage):
            pending.update(call["id"] for call in message.tool_calls)
        elif isinstance(message, ToolMessage):
            pending.discard(message.tool_call_id)
        if not pending:
            safe_index = position + 1
    return [RemoveMessage(id=message.id) for message in messages[:safe_index]
            if getattr(message, "id", None)]


def result_removals(messages: list, result: dict) -> list[RemoveMessage]:
    """迟到结果只删除仍匹配快照的连续前缀；变化时保留原文。"""
    requested = result.get("remove_ids", [])
    fingerprints = result.get("fingerprints", {})
    current = freeze_messages(messages)
    ids = [item["data"]["id"] for item in current]
    existing = set(ids)
    remaining = [message_id for message_id in requested if message_id in existing]
    if ids[:len(remaining)] != remaining:
        return []
    for item in current[:len(remaining)]:
        if fingerprints.get(item["data"]["id"]) != message_fingerprint(item):
            return []
    return history_removals(messages, len(remaining))
