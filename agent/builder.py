# agent/builder.py
# CLI 角色对话图：主回复、记忆检索、独立检查与状态更新。

from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from langchain_core.runnables import Runnable, RunnableLambda

from agent.node.memory import (
    create_prepare_memory_node, create_enqueue_memory_node,
    create_apply_memory_results_node,
)
from agent.tools import build_default_tools
from agent.node.check import check_judge, create_check_node
from agent.node.draft import commit_reply, create_draft_node, draft_judge, reply_failed
from agent.node.event import event_judge
from agent.node.world_state import update_world_state
from agent.node.participant_state import create_participant_state_node
from agent.classes.state import AgentState
from agent.node.state import begin_turn, increment_iteration
from agent.node.tts import create_tts_node


################# RP 主节点与回复检查 ####################

async def build_rp_agent(
    character_name: str,
    *,
    pool=None,
    checkpointer=None,
    memory_jobs=None,
    character_profile=None,
    before_node=None,
    on_commit=None,
    recovery_only=False,
    memory_retrieval_enabled=True,
    memory_storage_enabled=True,
    memory_source=None,
):
    """CLI 角色扮演 agent：主节点自主检索，独立检查后提交回复。

    begin_turn → apply_memory_results → update_world_state → participant_state_in
      → draft ⇄ tools(默认工具集)
      → check → commit_reply → participant_state_out → increment_iteration → tts
      → prepare_memory → enqueue_memory → END
    check 未通过时回到 draft 修订；工具消息直接保留在 messages 中。
    """
    memory_store = None
    if pool is not None and not recovery_only and (memory_retrieval_enabled or memory_storage_enabled):
        from agent.memory import AsyncPostgresCharacterMemoryStore
        memory_store = await AsyncPostgresCharacterMemoryStore.create(pool, character_name)
        if memory_jobs is None:
            from agent.memory.jobs import MemoryJobRepository
            memory_jobs = await MemoryJobRepository.create(pool)

    tools = build_default_tools(memory_store=memory_store if memory_retrieval_enabled else None) if not recovery_only else []

    from agent.utils.character import load_character_profile
    if character_profile is None and not recovery_only:
        character_profile = load_character_profile(character_name)

    workflow = StateGraph(AgentState)

    def add_node(name, factory):
        if recovery_only:
            workflow.add_node(name, lambda state: {})
            return
        node = factory()
        if before_node is None and on_commit is None:
            workflow.add_node(name, node)
            return
        runnable = node if isinstance(node, Runnable) else RunnableLambda(node)

        async def observed(state, config):
            if before_node is not None:
                await before_node(name, state)
            result = await runnable.ainvoke(state, config)
            if name == "commit_reply" and on_commit is not None:
                # Persist the checked reply before the graph may advance or trim history.
                await on_commit(state, result)
            return result
        workflow.add_node(name, observed)

    add_node("begin_turn", lambda: begin_turn)
    add_node("apply_memory_results", lambda: create_apply_memory_results_node(memory_jobs, character_name))
    from agent.node.context import limit_context
    add_node('limit_context', lambda: limit_context)
    add_node("world_state_update", lambda: update_world_state)
    add_node("participant_state_in", lambda: create_participant_state_node(trigger="user"))
    add_node("tools", lambda: ToolNode(tools))
    add_node("draft", lambda: create_draft_node(
        character_name=character_name, character_profile=character_profile, tools=tools))
    add_node("check", lambda: create_check_node())
    add_node("commit_reply", lambda: commit_reply)
    add_node("reply_failed", lambda: reply_failed)
    add_node("participant_state_out", lambda: create_participant_state_node(trigger="reply"))
    add_node("update_iter", lambda: increment_iteration)
    add_node("tts", lambda: create_tts_node(character_name=character_name))
    add_node("prepare_memory", lambda: create_prepare_memory_node(character_name, memory_source))
    add_node("enqueue_memory", lambda: create_enqueue_memory_node(memory_jobs))

    workflow.set_entry_point("begin_turn")
    workflow.add_edge("begin_turn", "apply_memory_results")
    workflow.add_edge("apply_memory_results", 'limit_context')
    workflow.add_edge('limit_context', "world_state_update")
    workflow.add_edge("world_state_update", "participant_state_in")
    workflow.add_edge("participant_state_in", "draft")
    workflow.add_edge("tools", "draft")
    workflow.add_conditional_edges(
        "draft", draft_judge,
        {"tools": "tools", "check": "check", "reply_failed": "reply_failed"},
    )
    workflow.add_conditional_edges(
        "check", check_judge,
        {"draft": "draft", "commit_reply": "commit_reply", "reply_failed": "reply_failed"},
    )
    workflow.add_edge("commit_reply", "participant_state_out")
    workflow.add_edge("reply_failed", END)
    workflow.add_edge("participant_state_out", "update_iter")
    workflow.add_edge("update_iter", "tts")
    workflow.add_conditional_edges(
        "tts",
        event_judge,
        {True: "prepare_memory", False: END},
    )
    workflow.add_edge("prepare_memory", "enqueue_memory")
    workflow.add_edge("enqueue_memory", END)

    return workflow.compile(checkpointer=checkpointer)


async def build_cli_agent(character_name: str, *, pool=None, checkpointer=None, memory_jobs=None):
    """CLI 端 agent 构建（单函数）：RP 主节点与回复检查。"""
    return await build_rp_agent(
        character_name=character_name,
        pool=pool,
        checkpointer=checkpointer,
        memory_jobs=memory_jobs,
    )
