"""图内记忆交接：持久化投递意图、入队、轮初应用已完成的裁剪结果。"""

from langchain_core.runnables import RunnableConfig

from agent.classes.state import AgentState
from agent.utils.memory import (
    build_memory_payload, freeze_messages,
    message_fingerprint, result_removals,
)
from utils.daily_logger import get_logger

__all__ = ["create_prepare_memory_node", "create_enqueue_memory_node",
           "create_apply_memory_results_node"]

logger = get_logger("memory.node")


def _thread_id(config: RunnableConfig) -> str:
    value = config.get("configurable", {}).get("thread_id")
    if not value:
        raise ValueError("后台记忆需要稳定的 thread_id")
    return str(value)


def create_prepare_memory_node(character_name: str, source=None):
    def prepare(state: AgentState, config: RunnableConfig):
        if not state.get('memory_storage_enabled', True):
            return {}
        if state.get("memory_pending_job") or state.get("memory_active_job"):
            return {}
        try:
            payload = build_memory_payload(state, character_name, _thread_id(config))
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("记忆快照边界无效 | error=%s", type(exc).__name__)
            return {"memory_warning": "记忆快照边界无效，保留原文和处理进度，本轮回复已完成。"}
        # 此节点输出先 checkpoint；下一节点才访问队列。
        return {"memory_pending_job": payload} if payload else {}
    if source is None:
        return prepare

    async def managed_prepare(state, config):
        if not state.get('memory_storage_enabled', True):
            return {}
        # Only authorized formal messages enter the memory model, including its background context.
        messages = await source(state)
        return prepare({**state, 'messages': messages}, config)
    return managed_prepare


async def _enqueue_pending(state, jobs):
    if not state.get('memory_storage_enabled', True):
        return {}
    payload = state.get("memory_pending_job")
    if not payload:
        return {}
    if jobs is None:
        return {"memory_warning": "记忆队列不可用，投递快照已保留，稍后补投。"}
    try:
        job_id = await jobs.enqueue(payload)
    except Exception as exc:
        logger.warning("记忆入队失败，保留快照 | error=%s", type(exc).__name__)
        return {"memory_warning": "记忆任务尚未入队，快照已保留，下一轮补投。"}
    return {
        "memory_pending_job": None, "memory_active_job": job_id,
        "memory_submitted_through": payload["through_message_id"],
        "iteration": max(0, state.get("iteration", 0) - payload["iteration"]),
        "memory_warning": "",
    }


def create_enqueue_memory_node(jobs):
    async def enqueue(state: AgentState):
        return await _enqueue_pending(state, jobs)
    return enqueue


def create_apply_memory_results_node(jobs, character_name: str):
    async def apply_results(state: AgentState, config: RunnableConfig):
        if not state.get('memory_storage_enabled', True):
            return {}
        if jobs is None:
            return {}
        if not config.get("configurable", {}).get("thread_id"):
            return {}
        thread_id = _thread_id(config)
        last_applied = state.get("memory_last_applied_job", 0)
        updates = {}
        skipped = ""
        try:
            # 输入中的标记已随上一轮裁剪落盘，且刚经过 begin_turn checkpoint。
            # 只确认旧标记，不在返回本次 RemoveMessage 前清除/确认本次结果。
            if last_applied:
                await jobs.acknowledge(character_name, thread_id, last_applied)
            updates.update(await _enqueue_pending(state, jobs))
            results = await jobs.results_after(character_name, thread_id, last_applied)
            current = list(state.get("messages", []))
            removed = []
            active = updates.get("memory_active_job", state.get("memory_active_job"))
            for result in results:
                through = result["through_message_id"]
                if 'memory_policy_version' in state:
                    # Managed snapshots come from the durable formal-message archive.
                    # Context trimming is independent; an evicted graph message is not a conflict.
                    updates.update(memory_last_applied_job=result['job_id'],
                                   memory_processed_through=through,
                                   memory_processed_fingerprint=result['through_fingerprint'],
                                   memory_warning='')
                    if active and result['job_id'] >= active:
                        active = None
                        updates['memory_active_job'] = None
                    continue
                snapshot = freeze_messages(current)
                by_id = {item["data"]["id"]: item for item in snapshot}
                fingerprints = result.get("fingerprints", {})
                invalid = through not in by_id or any(
                    message_fingerprint(by_id[mid]) != fingerprint
                    for mid, fingerprint in fingerprints.items() if mid in by_id
                )
                if not invalid:
                    invalid = message_fingerprint(by_id[through]) != result["through_fingerprint"]
                removals = [] if invalid else result_removals(current, result)
                # 结果已由 Worker 原子提交；历史变化时只跳过其裁剪，仍确认该结果，避免阻塞后续同步。
                reason = ""
                if invalid:
                    reason = (f"记忆结果 {result['job_id']} 对应的历史已变化，"
                              "已保留原文并跳过本次裁剪，继续同步后续结果。")
                elif any(mid in by_id for mid in result.get("remove_ids", [])) and not removals:
                    reason = (f"记忆结果 {result['job_id']} 的裁剪边界不再安全，"
                              "已保留原文并跳过本次裁剪，继续同步后续结果。")
                if reason:
                    skipped = reason
                    logger.warning("记忆结果跳过应用 | job_id=%s through=%s invalid=%s",
                                   result["job_id"], through, invalid)
                    updates["memory_last_applied_job"] = result["job_id"]
                    if active and result["job_id"] >= active:
                        active = None
                        updates["memory_active_job"] = None
                    continue
                ids = {item.id for item in removals}
                current = [message for message in current if message.id not in ids]
                removed.extend(removals)
                if removed:
                    updates["messages"] = list(removed)
                updates.update(
                    memory_last_applied_job=result["job_id"],
                    memory_processed_through=through,
                    memory_processed_fingerprint=result["through_fingerprint"],
                    memory_warning="",
                )
                logger.info("记忆任务执行成功并应用结果 | job_id=%s through=%s", result["job_id"], through)
                if removals:
                    updates["memory_trimmed_through"] = removals[-1].id
                if active and result["job_id"] >= active:
                    active = None
                    updates["memory_active_job"] = None
            if skipped:
                updates["memory_warning"] = skipped
            if active and await jobs.job_status(active) == "failed":
                updates["memory_warning"] = f"记忆任务 {active} 重试耗尽，原始对话仍保留，可单独重试该任务。"
        except Exception as exc:
            logger.warning("轮初记忆同步失败 | error=%s", type(exc).__name__)
            updates["memory_warning"] = "记忆结果暂时无法同步，继续使用已有对话。"
        return updates
    return apply_results
