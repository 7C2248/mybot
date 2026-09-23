"""固定快照的记忆计算：检索、总结、预编码，数据库写入由 Worker 统一提交。"""

import asyncio
import json

from langchain_core.messages import HumanMessage, SystemMessage, messages_from_dict

from agent.classes.memory_job import MemoryOperation, MemoryPlan
from agent.utils.memory import history_removals
from agent.utils.models import get_node_model, get_qwen_embedding_model
from agent.utils.messages import format_history
from agent.utils.state import prepare_world_state
from agent.prompts import get_prompt
from agent.utils.text import strip_code_fence
from utils.daily_logger import get_logger

logger = get_logger("memory.processor")

def _parse_memory_queries(content: str) -> list[dict]:
    """解析 query LLM 输出为 [{query, date_from, date_to}, ...]。

    新格式元素为对象 {"query": str, "date_from"?, "date_to"?}，
    兼容旧格式（纯字符串数组）。解析失败返回空列表。
    """
    content = (content or "").strip()
    content = strip_code_fence(content)

    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        logger.warning(f"生成检索语句失败: {content!r}")
        return []

    queries = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"query": item}
        if not isinstance(item, dict):
            continue
        query_text = (item.get("query") or "").strip()
        if not query_text:
            continue
        queries.append({
            "query": query_text,
            "date_from": item.get("date_from"),
            "date_to": item.get("date_to"),
        })
    return queries


async def _retrieve_memories(memory_store, queries: list[dict],
                             final_limit: int) -> list:
    """对每条查询执行混合检索，按 id 去重合并。"""
    if not queries:
        return []
    related_memories = []
    seen_ids = set()
    encoder = await asyncio.to_thread(get_qwen_embedding_model)
    for query in queries:
        embedding = await asyncio.to_thread(encoder.encode, query["query"], prompt_name="query")
        rows = await memory_store.search_hybrid(
            embedding,
            query_text=query["query"],
            keyword_text=query["query"],
            date_from=query["date_from"],
            date_to=query["date_to"],
            final_limit=final_limit,
        )
        for row in rows:
            if row["id"] not in seen_ids:
                seen_ids.add(row["id"])
                related_memories.append(row)
    return related_memories


_MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "insert_memory",
            "description": "Add a new memory to the character's long-term memory bank. Use only when the information extracted from the conversation has high long-term value and does not duplicate or conflict with existing memories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory": {
                        "type": "string",
                        "description": "The memory text to be stored. It should be concise, semantically complete, and avoid colloquial or temporary descriptions.",
                    },
                    "importance": {
                        "type": "integer",
                        "description": "The importance level of this memory for retrieval ordering. Higher values mean more important (default 0, maximum 100). Set higher for core character traits, critical plot events, or key relationship milestones.",
                    },
                },
                "required": ["memory", "importance"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_memory",
            "description": "Update an existing memory entry in the database. Use when new information conflicts with an old memory, the old memory is outdated, or corrections are needed based on the latest plot developments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "integer",
                        "description": "The unique ID of the memory entry to be updated.",
                    },
                    "new_memory": {
                        "type": "string",
                        "description": "The new memory text that replaces the old content. It should integrate old and new information, maintain semantic coherence, and be consistent with the character's current state.",
                    },
                    "importance": {
                        "type": "integer",
                        "description": "The importance level of this memory for retrieval ordering. Higher values mean more important (default 0, maximum 100). Set higher for core character traits, critical plot events, or key relationship milestones.",
                    },
                },
                "required": ["memory_id", "new_memory"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_memory",
            "description": "Completely delete the memory with the specified ID from the memory bank. Use only when the memory is confirmed to be incorrect, severely redundant, or completely irrelevant to the character's core settings / plot direction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "integer",
                        "description": "The unique ID of the memory entry to be deleted.",
                    }
                },
                "required": ["memory_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clean_history",
            "description": """Remove the first i messages from the raw message queue.
After this operation, the main LLM will no longer see those messages; only the summarized memory will remain.
Use when a meaningful event has been completed and you want to discard the raw dialogue details to save context, but be careful not to cut off an unresolved emotional arc or ongoing subtle tension.
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "The number of messages to remove from the beginning of the message queue. Must be a positive integer, and retain at least 6 messages to prevent context loss.",
                    }
                },
                "required": ["index"],
            },
        },
    },
]


async def process_memory_snapshot(payload: dict, memory_store) -> MemoryPlan:
    """只计算变更计划。旧消息可用于理解背景，只有新增范围参与事实整理。"""
    if payload.get("version") != 1:
        raise ValueError("不支持的记忆任务协议版本")
    if not payload.get('memory_storage_enabled', True):
        raise ValueError('记忆存储未开启')
    retrieval = payload.get('memory_retrieval_enabled', True)
    messages = messages_from_dict(payload["messages"])
    start = payload["new_message_start"]
    if not 0 <= start < len(messages):
        raise ValueError("记忆任务新增消息范围无效")
    _, state_text = prepare_world_state(payload.get("world_state"))
    related = []
    if retrieval:
        query_context = messages[max(0, start - 6):]
        response = await get_node_model("memory_query").ainvoke([
            SystemMessage(content=get_prompt("memory_query")),
            HumanMessage(content=state_text + "\n" + format_history(query_context[-120:])),
        ])
        queries = _parse_memory_queries(getattr(response, "content", ""))
        related = await _retrieve_memories(memory_store, queries, final_limit=12)
    known_ids = {row["id"] for row in related}
    memories = "<memories>\n" + "\n".join(
        f"{row['id']},{row['importance']}: {row['memory']}" for row in related
    ) + "\n</memories>\n"
    scope = (
        f"本次新增消息从 history_messages 的 index:{start} 开始，直到末尾。\n"
        "更早的消息已经完成记忆整理，仅用于理解指代和背景；不要仅依据旧消息再次插入记忆。\n"
        "新增消息可以补充、纠正已有记忆。clean_history 的 index 仍按完整 history_messages 编号，"
        "可以裁剪已经完成整理的旧历史，保留未结束事件和至少六条消息。"
    )
    tools = _MEMORY_TOOLS if retrieval else [t for t in _MEMORY_TOOLS if t['function']['name'] in ('insert_memory', 'clean_history')]
    if not retrieval:
        scope += '\n本会话关闭了记忆检索。只提取新增记忆，不推断旧记忆内容，不更新或删除旧记录。'
    response = await get_node_model("memory_summary").bind_tools(tools).ainvoke([
        SystemMessage(content=get_prompt("event_summary")),
        SystemMessage(content=scope),
        HumanMessage(content=state_text), HumanMessage(content=memories),
        HumanMessage(content=format_history(messages, include_tools=True)),
    ])
    if getattr(response, "invalid_tool_calls", []):
        raise ValueError("记忆工具调用参数无效")
    # 先校验全部工具调用，避免参数错误时进行不必要的切块/编码。
    writes = []
    touched = set()
    trim_index = None
    for call in response.tool_calls:
        name, args = call["name"], call["args"]
        if name == "clean_history":
            trim_index = args.get("index")
            if type(trim_index) is not int or trim_index <= 0:
                raise ValueError("clean_history.index 必须是正整数")
            continue
        if name not in ("insert_memory", "update_memory", "delete_memory"):
            raise ValueError("未知记忆工具调用")
        memory_id = args.get("memory_id")
        if name != "insert_memory":
            if not retrieval:
                raise ValueError('关闭检索时只允许新增记忆')
            if type(memory_id) is not int or memory_id not in known_ids:
                raise ValueError("只能修改本次检索得到的角色记忆")
            if memory_id in touched:
                raise ValueError("同一计划不能多次修改同一条记忆")
            touched.add(memory_id)
        text = args.get("memory" if name == "insert_memory" else "new_memory")
        importance = args.get("importance", 0 if name == "insert_memory" else None)
        if name != "delete_memory":
            if not isinstance(text, str) or not text.strip():
                raise ValueError("记忆内容不能为空")
            if importance is not None and (type(importance) is not int or not 0 <= importance <= 100):
                raise ValueError("记忆重要性必须是 0 到 100 的整数")
        writes.append((name, memory_id, text, importance))
    plan = MemoryPlan(remove_ids=[item.id for item in history_removals(messages, trim_index)])
    for name, memory_id, text, importance in writes:
        prepared = None if name == "delete_memory" else await memory_store.prepare_memory(text, importance)
        plan.operations.append(MemoryOperation(name, memory_id, prepared))
    return plan
