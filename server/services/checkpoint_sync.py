"""Phase 1 checkpoint reload: truncate the UI tail to the latest checkpoint message.

The checkpoint is authoritative for the agent context, but it is not a complete
archive. Only messages newer than the matched checkpoint tail are removed; older
messages that context trimming evicted from the checkpoint stay in the UI.
"""

from datetime import datetime, timezone

from server.classes.api import ServiceError
from server.repositories.threads import ThreadRepository
from server.services.legacy import checkpoint_messages
from utils.daily_logger import get_logger, log_event

logger = get_logger("server.checkpoint")


class CheckpointSyncService:
    def __init__(self, database):
        self.database = database

    async def sync(self, thread_id, body):
        if self.database.pool is None:
            raise ServiceError("database_unavailable", "数据库尚未就绪。", 503)
        repository = ThreadRepository(self.database.pool)
        result = await repository.sync_checkpoint(thread_id, body.expected_version, dry_run=body.dry_run,
                                                  checkpoint_id=body.checkpoint_id, read=self._read)
        log_event(logger, "检查点同步", thread_id=thread_id, checkpoint_id=result["checkpoint_id"],
                  dry_run=result["dry_run"], deleted_messages=result["delete_count"],
                  deleted_runs=result["run_count"])
        return result

    async def _read(self, conn, thread):
        """Read the latest checkpoint (or the import seed) inside the caller's transaction."""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from server.services.agent import public_state, thaw_state

        saver = AsyncPostgresSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=None))
        value = await saver.aget_tuple({"configurable": {"thread_id": thread["graph_thread_id"], "checkpoint_ns": ""}})
        if value is None:
            if not thread["import_state"]:
                raise ServiceError("checkpoint_missing", "该会话还没有可用的检查点，无法重新加载。", 409)
            values = thaw_state(thread["import_state"])
            checkpoint_id = None
            stamp = datetime.now(timezone.utc)
        else:
            values = value.checkpoint["channel_values"]
            checkpoint_id = value.checkpoint["id"]
            stamp = datetime.fromisoformat(value.checkpoint["ts"])
        items, _ = checkpoint_messages(values, stamp, ai_prefixes=("reply_", "legacy_"))
        pending = values.get("memory_pending_job") or {}
        return {
            "checkpoint_id": checkpoint_id,
            "messages": items,
            "state": public_state(values),
            "markers": {
                "processed": values.get("memory_processed_through") or None,
                "submitted": values.get("memory_submitted_through") or None,
                "pending": (pending.get("through_message_id") if isinstance(pending, dict) else None) or None,
                "active": bool(values.get("memory_active_job")),
            },
        }
