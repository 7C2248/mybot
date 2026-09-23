"""UI history and durable runs. Every externally visible transition is transactional."""

import base64
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import json
from uuid import UUID, uuid4, uuid5

from psycopg.types.json import Jsonb

from server.classes.api import ServiceError
from server.classes.runs import Message
from utils.daily_logger import get_logger, log_event

logger = get_logger("server.runs")

TERMINAL = {"completed", "completed_with_warnings", "failed", "interrupted"}


def _encode(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode(cursor, kind, scope):
    try:
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if not isinstance(value, list) or len(value) != 4 or value[:3] != [1, kind, scope]:
            raise ValueError()
        return value[3]
    except (ValueError, TypeError, UnicodeError):
        raise ServiceError("invalid_cursor", "分页游标无效。") from None


class RunRepository:
    def __init__(self, pool, *, connection=None, connection_lock=None):
        self.pool = pool
        # A worker uses its advisory-lock connection for all writes. It must never
        # reconnect and publish results after losing ownership of this session.
        self.worker_connection = connection
        self.connection_lock = connection_lock

    @asynccontextmanager
    async def connection(self):
        if self.worker_connection is not None:
            if self.connection_lock is not None:
                async with self.connection_lock:
                    yield self.worker_connection
            else:
                yield self.worker_connection
        else:
            async with self.pool.connection() as conn:
                yield conn

    async def _thread(self, conn, thread_id, *, lock=False, include_deleted=False):
        row = await (await conn.execute("SELECT * FROM mybot_ui.threads WHERE id = %s" +
                                      (" FOR UPDATE" if lock else ""), (thread_id,))).fetchone()
        if row is None or (row["deleted_at"] is not None and not include_deleted):
            raise ServiceError("thread_not_found", "会话不存在。", 404)
        return row

    async def _run(self, conn, run_id, *, lock=False):
        row = await (await conn.execute("SELECT r.* FROM mybot_ui.runs r JOIN mybot_ui.threads t ON t.id = r.thread_id WHERE r.id = %s AND t.deleted_at IS NULL" +
                                      (" FOR UPDATE OF r" if lock else ""), (run_id,))).fetchone()
        if row is None:
            raise ServiceError("run_not_found", "运行不存在。", 404)
        return row

    async def _event(self, conn, run_id, event_type, payload):
        # Caller holds the run row lock (or has inserted it in this transaction).
        return await (await conn.execute("""
            INSERT INTO mybot_ui.run_events(run_id, sequence, type, payload)
            SELECT %s, COALESCE(MAX(sequence), 0) + 1, %s, %s FROM mybot_ui.run_events WHERE run_id = %s
            RETURNING *
        """, (run_id, event_type, Jsonb(payload), run_id))).fetchone()

    async def create_thread(self, character_id, title, *, memory_retrieval_enabled=False,
                            memory_storage_enabled=False, source='desktop'):
        thread_id = uuid4()
        async with self.connection() as conn:
            return await (await conn.execute("""
                INSERT INTO mybot_ui.threads(id, character_id, graph_thread_id, title,
                    memory_retrieval_enabled, memory_storage_enabled, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *
            """, (thread_id, character_id, f"ui:{thread_id}", title,
                  memory_retrieval_enabled, memory_storage_enabled, source))).fetchone()

    async def get_thread(self, thread_id):
        async with self.connection() as conn:
            thread = await self._thread(conn, thread_id)
            latest = await (await conn.execute("SELECT to_jsonb(r) - 'base_state' AS value FROM mybot_ui.runs r WHERE thread_id = %s ORDER BY created_at DESC, id DESC LIMIT 1", (thread_id,))).fetchone()
            current = await (await conn.execute("SELECT to_jsonb(r) - 'base_state' AS value FROM mybot_ui.runs r WHERE thread_id = %s AND status IN ('queued', 'running') LIMIT 1", (thread_id,))).fetchone()
            return {**thread, 'current_run': current['value'] if current else None, 'latest_run': latest['value'] if latest else None}

    async def list_threads(self, *, cursor=None, limit=30, deleted=False):
        params = []
        scope = 'deleted' if deleted else 'all'
        where = ' WHERE t.deleted_at IS NOT NULL' if deleted else ' WHERE t.deleted_at IS NULL'
        if cursor:
            value = _decode(cursor, "threads", scope)
            try:
                stamp, identifier = value
                timestamp = datetime.fromisoformat(stamp)
                if timestamp.tzinfo is None:
                    raise ValueError()
                params = [timestamp, UUID(identifier)]
            except (ValueError, TypeError, AttributeError):
                raise ServiceError("invalid_cursor", "分页游标无效。") from None
            where += " AND (t.created_at, t.id) < (%s, %s)"
        async with self.connection() as conn:
            rows = await (await conn.execute("""
                SELECT t.*, (SELECT to_jsonb(r) - 'base_state' FROM mybot_ui.runs r
                             WHERE r.thread_id = t.id AND r.status IN ('queued', 'running') LIMIT 1) AS current_run,
                            (SELECT to_jsonb(r) - 'base_state' FROM mybot_ui.runs r
                             WHERE r.thread_id = t.id ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS latest_run
                FROM mybot_ui.threads t
            """ + where + " ORDER BY t.created_at DESC, t.id DESC LIMIT %s", (*params, limit + 1))).fetchall()
        next_cursor = _encode([1, "threads", scope, [rows[limit-1]["created_at"].isoformat(), str(rows[limit-1]["id"])]]) if len(rows) > limit else None
        return {"items": rows[:limit], "next_cursor": next_cursor}

    async def messages(self, thread_id, *, before=None, limit=30):
        params = [thread_id]
        where = ""
        if before:
            value = _decode(before, "messages", str(thread_id))
            if type(value) is not int or not 0 < value <= 9223372036854775807:
                raise ServiceError("invalid_cursor", "分页游标无效。")
            where = " AND sequence < %s"
            params.append(value)
        async with self.connection() as conn:
            await self._thread(conn, thread_id)
            rows = await (await conn.execute("SELECT * FROM mybot_ui.messages WHERE thread_id = %s" + where +
                                            " ORDER BY sequence DESC LIMIT %s", (*params, limit + 1))).fetchall()
        next_cursor = _encode([1, "messages", str(thread_id), rows[limit-1]["sequence"]]) if len(rows) > limit else None
        return {"items": list(reversed(rows[:limit])), "next_cursor": next_cursor}

    async def submit(self, thread_id, text, request_id, *, retry_of=None):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        async with self.connection() as conn:
            async with conn.transaction():
                thread = await self._thread(conn, thread_id, lock=True)
                existing = await (await conn.execute("SELECT * FROM mybot_ui.runs WHERE thread_id = %s AND client_request_id = %s",
                                                    (thread_id, request_id))).fetchone()
                if existing:
                    if existing["payload_hash"] != digest or existing["retry_of"] != retry_of:
                        raise ServiceError("request_conflict", "请求键已用于其他输入或重试。", 409)
                    return existing
                busy = await (await conn.execute("SELECT id FROM mybot_ui.runs WHERE thread_id = %s AND status IN ('queued', 'running')",
                                                (thread_id,))).fetchone()
                if busy:
                    raise ServiceError("thread_busy", "该会话仍有未完成运行。", 409, details={"run_id": str(busy["id"])})
                run_id = uuid4()
                message_id = uuid4()
                base = None
                retrieval = thread['memory_retrieval_enabled']
                storage = thread['memory_storage_enabled']
                policy_version = thread['memory_policy_version']
                if retry_of:
                    previous = await self._run(conn, retry_of, lock=True)
                    if previous["thread_id"] != thread_id or previous["status"] not in ("failed", "interrupted"):
                        raise ServiceError("retry_not_allowed", "此运行不能重试。", 409)
                    committed = await (await conn.execute("SELECT 1 FROM mybot_ui.messages WHERE run_id = %s AND role = 'assistant'",
                                                         (retry_of,))).fetchone()
                    last = await (await conn.execute("SELECT id FROM mybot_ui.runs WHERE thread_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
                                                    (thread_id,))).fetchone()
                    if committed or last["id"] != retry_of:
                        raise ServiceError("retry_not_allowed", "只能重试尚未提交正文的最近一次失败运行。", 409)
                    message_id = previous["user_message_id"]
                    base = previous["base_state"]
                    retrieval = retrieval and previous['memory_retrieval_enabled']
                    storage = storage and previous['memory_storage_enabled'] and policy_version == previous['memory_policy_version']
                await conn.execute("""
                    INSERT INTO mybot_ui.runs(id, thread_id, user_message_id, client_request_id, payload_hash, retry_of, base_state,
                        memory_retrieval_enabled, memory_storage_enabled, memory_policy_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (run_id, thread_id, message_id, request_id, digest, retry_of, Jsonb(base) if base is not None else None, retrieval, storage, policy_version))
                if not retry_of:
                    await conn.execute("""
                        INSERT INTO mybot_ui.messages(id, thread_id, run_id, sequence, role, text, graph_message_id)
                        SELECT %s, %s, %s, COALESCE(MAX(sequence), 0) + 1, 'user', %s, %s
                        FROM mybot_ui.messages WHERE thread_id = %s
                    """, (message_id, thread_id, run_id, text, f"user_{message_id}", thread_id))
                await conn.execute("UPDATE mybot_ui.threads SET updated_at = now() WHERE id = %s", (thread_id,))
                await self._event(conn, run_id, "phase", {"phase": "queued"})
                run = await self._run(conn, run_id)
        log_event(logger, "对话任务入队", run_id=run_id, thread_id=thread_id, character=thread["character_id"],
                  message_id=message_id, retry_of=retry_of, input_chars=len(text),
                  memory_retrieval_enabled=retrieval, memory_storage_enabled=storage)
        return run

    async def retry(self, run_id, request_id):
        async with self.connection() as conn:
            previous = await self._run(conn, run_id)
            text = (await (await conn.execute("SELECT text FROM mybot_ui.messages WHERE id = %s",
                                             (previous["user_message_id"],))).fetchone())["text"]
        return await self.submit(previous["thread_id"], text, request_id, retry_of=run_id)

    async def snapshot(self, run_id):
        async with self.connection() as conn:
            async with conn.transaction():
                run = await self._run(conn, run_id, lock=True)
                run["messages"] = await (await conn.execute("SELECT * FROM mybot_ui.messages WHERE id = %s OR (run_id = %s AND role = 'assistant') ORDER BY sequence",
                                                           (run["user_message_id"], run_id))).fetchall()
                run["last_event_sequence"] = (await (await conn.execute("SELECT COALESCE(MAX(sequence), 0) AS n FROM mybot_ui.run_events WHERE run_id = %s", (run_id,))).fetchone())["n"]
                return run

    async def events(self, run_id, after=0, limit=100):
        async with self.connection() as conn:
            await self._run(conn, run_id)
            return await (await conn.execute("SELECT * FROM mybot_ui.run_events WHERE run_id = %s AND sequence > %s ORDER BY sequence LIMIT %s",
                                            (run_id, after, limit))).fetchall()

    async def claim(self):
        async with self.connection() as conn:
            async with conn.transaction():
                run = await (await conn.execute("SELECT * FROM mybot_ui.runs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED")).fetchone()
                if run is None:
                    return None
                await conn.execute("UPDATE mybot_ui.runs SET status = 'running', phase = 'preparing', started_at = now() WHERE id = %s", (run["id"],))
                await self._event(conn, run["id"], "run.started", {"phase": "preparing"})
                context = await self.context(conn, run["id"])
        log_event(logger, "对话任务开始", run_id=run["id"], thread_id=run["thread_id"],
                  character=context["character_id"])
        return context

    async def context(self, conn, run_id):
        return await (await conn.execute("""
            SELECT r.*, t.character_id, t.graph_thread_id, t.recovery_run_id, t.import_state,
                   m.text, m.created_at AS input_created_at
            FROM mybot_ui.runs r JOIN mybot_ui.threads t ON t.id = r.thread_id
            JOIN mybot_ui.messages m ON m.id = r.user_message_id WHERE r.id = %s
        """, (run_id,))).fetchone()

    async def save_base(self, run_id, state, model_version, profile_version):
        async with self.connection() as conn:
            await conn.execute("UPDATE mybot_ui.runs SET base_state = %s, model_version = %s, profile_version = %s WHERE id = %s AND status = 'running'",
                               (Jsonb(state), model_version, profile_version, run_id))

    async def publish(self, run_id, event_type, payload):
        async with self.connection() as conn:
            async with conn.transaction():
                run = await self._run(conn, run_id, lock=True)
                if run["status"] != "running":
                    raise ServiceError("run_not_active", "运行已结束。", 409)
                if event_type == "phase":
                    await conn.execute("UPDATE mybot_ui.runs SET phase = %s WHERE id = %s", (payload["phase"], run_id))
                elif event_type == "state.updated":
                    await conn.execute("UPDATE mybot_ui.threads SET state = state || %s WHERE id = %s", (Jsonb(payload), run["thread_id"]))
                elif event_type == "memory.retrieved":
                    await conn.execute("UPDATE mybot_ui.threads SET state = jsonb_set(state, '{retrieved_memories}', COALESCE(state->'retrieved_memories', '[]'::jsonb) || %s) WHERE id = %s",
                                       (Jsonb(payload["hits"]), run["thread_id"]))
                await self._event(conn, run_id, event_type, payload)
        if event_type == "phase":
            log_event(logger, "对话阶段变更", run_id=run_id, thread_id=run["thread_id"], phase=payload["phase"])
        elif event_type == "memory.retrieved":
            log_event(logger, "对话记忆检索完成", run_id=run_id, thread_id=run["thread_id"],
                      memory_ids=[hit["id"] for hit in payload["hits"]])

    async def commit_reply(self, run_id, text, graph_message_id):
        async with self.connection() as conn:
            async with conn.transaction():
                # Match HTTP lock order: thread first, then run.
                run = await self._run(conn, run_id)
                thread = await self._thread(conn, run["thread_id"], lock=True)
                run = await self._run(conn, run_id, lock=True)
                existing = await (await conn.execute("SELECT * FROM mybot_ui.messages WHERE run_id = %s AND role = 'assistant'", (run_id,))).fetchone()
                if existing:
                    if existing["text"] != text or existing["graph_message_id"] != graph_message_id:
                        raise ServiceError("reply_conflict", "该运行已保存不同的正式回复。", 409)
                    return existing
                if run["status"] != "running":
                    raise ServiceError("run_not_active", "运行已结束。", 409)
                message_id = uuid5(run_id, "assistant")
                message = await (await conn.execute("""
                    INSERT INTO mybot_ui.messages(id, thread_id, run_id, sequence, role, text, graph_message_id)
                    SELECT %s, %s, %s, COALESCE(MAX(sequence), 0) + 1, 'assistant', %s, %s
                    FROM mybot_ui.messages WHERE thread_id = %s RETURNING *
                """, (message_id, run["thread_id"], run_id, text, graph_message_id, run["thread_id"]))).fetchone()
                await self._event(conn, run_id, "message.committed", {"message": Message.model_validate(message).model_dump(mode="json")})
                await conn.execute("UPDATE mybot_ui.threads SET updated_at = now() WHERE id = %s", (run["thread_id"],))
        log_event(logger, "正式回复已提交", run_id=run_id, thread_id=run["thread_id"],
                  character=thread["character_id"], message_id=message["id"],
                  graph_message_id=graph_message_id, text=message["text"])
        return message

    async def finish(self, run_id, *, error=None, warning=None, interrupted=False, needs_recovery=False):
        async with self.connection() as conn:
            async with conn.transaction():
                run = await self._run(conn, run_id)
                await self._thread(conn, run["thread_id"], lock=True)
                run = await self._run(conn, run_id, lock=True)
                if run["status"] in TERMINAL:
                    return
                committed = await (await conn.execute("SELECT 1 FROM mybot_ui.messages WHERE run_id = %s AND role = 'assistant'", (run_id,))).fetchone()
                warnings = list(run["warnings"])
                if warning:
                    warnings.append(warning)
                if committed:
                    if error:
                        warnings.append("postprocessing_interrupted")
                    status = "completed_with_warnings" if warnings else "completed"
                    error = None
                else:
                    status = "interrupted" if interrupted else "failed"
                    error = error or "reply_failed"
                await conn.execute("UPDATE mybot_ui.runs SET status = %s, phase = %s, error_code = %s, warnings = %s, finished_at = now() WHERE id = %s",
                                   (status, status, error, Jsonb(list(dict.fromkeys(warnings))), run_id))
                await conn.execute("UPDATE mybot_ui.threads SET updated_at = now(), recovery_run_id = %s WHERE id = %s",
                                   (run_id if needs_recovery else None, run["thread_id"]))
                await self._event(conn, run_id, "run.completed" if committed else "run.failed",
                                  {"status": status, "error_code": error, "warnings": list(dict.fromkeys(warnings))})
        log_event(logger, "对话任务结束", run_id=run_id, thread_id=run["thread_id"], status=status,
                  error_code=error, warnings=list(dict.fromkeys(warnings)), needs_recovery=needs_recovery)

    async def recovery_runs(self):
        async with self.connection() as conn:
            rows = await (await conn.execute("SELECT id FROM mybot_ui.runs WHERE status = 'running' ORDER BY created_at, id")).fetchall()
            return [await self.context(conn, row["id"]) for row in rows]

    async def recovery_context(self, run_id):
        async with self.connection() as conn:
            return await self.context(conn, run_id)

    async def clear_recovery(self, thread_id):
        async with self.connection() as conn:
            await conn.execute("UPDATE mybot_ui.threads SET recovery_run_id = NULL WHERE id = %s", (thread_id,))

    async def memory_messages(self, thread_id, policy_version, after=None):
        from langchain_core.messages import HumanMessage, AIMessage
        async with self.connection() as conn:
            rows = await (await conn.execute("""
                SELECT m.* FROM mybot_ui.messages m LEFT JOIN mybot_ui.runs r ON r.id = m.run_id
                WHERE m.thread_id = %s
                  AND COALESCE(r.memory_storage_enabled, m.detached_memory_storage_enabled, false)
                  AND COALESCE(r.memory_policy_version, m.detached_memory_policy_version) = %s
                  AND m.sequence >= COALESCE((SELECT sequence FROM mybot_ui.messages
                      WHERE thread_id = %s AND graph_message_id = %s), 0)
                ORDER BY m.sequence
            """, (thread_id, policy_version, thread_id, after))).fetchall()
        return [(HumanMessage if r['role'] == 'user' else AIMessage)(
            id=r['graph_message_id'], content=f"<timestamp>{r['created_at'].isoformat()}</timestamp>\n" + r['text']) for r in rows]
