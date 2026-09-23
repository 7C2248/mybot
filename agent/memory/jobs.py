"""PostgreSQL 持久化队列；单消费者锁、结果回执和原子完成。"""

from contextlib import asynccontextmanager
from pathlib import Path

from psycopg.types.json import Jsonb

from agent.utils.memory import message_fingerprint
from agent.memory.policy import memory_permitted
from utils.daily_logger import get_logger, log_event, log_failure

logger = get_logger("memory.jobs")


class MemoryBusyError(RuntimeError):
    pass


class MemoryJobRepository:
    # 会话级锁覆盖一次完整计算及提交。进程/连接断开即释放，无 running 租约。
    _WORKER_LOCK = (1835363695, 1)

    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def create(cls, pool):
        instance = cls(pool)
        await instance.setup()
        return instance

    async def setup(self):
        migration = Path(__file__).resolve().parents[0] / "migrations" / "001_memory_jobs.sql"
        async with self.pool.connection() as conn:
            async with conn.transaction():
                # CLI 与 Worker 可同时启动；串行执行幂等 DDL。
                await conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (1835363695, 2))
                await conn.execute(migration.read_text(encoding="utf-8"))

    async def _lock_stream(self, conn, character_name, thread_id):
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           (f"memory-stream:{character_name}:{thread_id}",))

    async def enqueue(self, payload: dict) -> int:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                if not await memory_permitted(conn, payload, lock=True):
                    raise MemoryBusyError('该会话已撤销此记忆任务')
                await self._lock_stream(conn, payload["character_name"], payload["thread_id"])
                cur = await conn.execute(
                    "SELECT job_id FROM memory_service.results WHERE job_key = %s",
                    (payload["job_key"],))
                receipt = await cur.fetchone()
                if receipt:
                    return receipt["job_id"]
                cur = await conn.execute(
                    "SELECT id, job_key FROM memory_service.jobs "
                    "WHERE character_name = %s AND thread_id = %s",
                    (payload["character_name"], payload["thread_id"]))
                existing = await cur.fetchone()
                if existing:
                    if existing["job_key"] != payload["job_key"]:
                        raise MemoryBusyError("该会话还有未完成的记忆任务")
                    return existing["id"]
                cur = await conn.execute(
                    "INSERT INTO memory_service.jobs (job_key, character_name, thread_id, payload) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (payload["job_key"], payload["character_name"], payload["thread_id"], Jsonb(payload)))
                job_id = (await cur.fetchone())["id"]
        log_event(logger, "记忆任务入队", job_id=job_id, character=payload["character_name"],
                  thread_id=payload["thread_id"], through_message_id=payload["through_message_id"],
                  message_count=len(payload["messages"]))
        return job_id

    @asynccontextmanager
    async def worker_session(self):
        """同一数据库只允许一个正在处理任务的 Worker；备用进程返回 None。"""
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s, %s) AS locked", self._WORKER_LOCK)
            locked = (await cur.fetchone())["locked"]
            try:
                yield conn if locked else None
            finally:
                if locked and not conn.closed:
                    await conn.execute("SELECT pg_advisory_unlock(%s, %s)", self._WORKER_LOCK)

    async def next_job(self, conn) -> dict | None:
        # 只在领取时开启短事务。任务一直留在表中，成功提交时才删除。
        # 较老的失败/退避任务阻止同角色后续任务越过它；其他角色仍能执行。
        async with conn.transaction():
            cur = await conn.execute("""
                SELECT j.* FROM memory_service.jobs j
                WHERE j.status = 'pending' AND j.next_attempt_at <= now()
                  AND NOT EXISTS (
                    SELECT 1 FROM memory_service.jobs older
                    WHERE older.character_name = j.character_name AND older.id < j.id
                  )
                ORDER BY j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED
            """)
            return await cur.fetchone()

    async def finish(self, conn, job: dict, store, plan) -> bool:
        """使用持有消费者锁的同一连接；写记忆、结果、删队列一起提交。"""
        from pgvector.psycopg import register_vector_async

        await register_vector_async(conn)
        changes = []
        async with conn.transaction():
            if not await memory_permitted(conn, job['payload'], lock=True):
                await conn.execute('DELETE FROM memory_service.jobs WHERE id = %s', (job['id'],))
                return False
            await self._lock_stream(conn, job["character_name"], job["thread_id"])
            cur = await conn.execute("SELECT id FROM memory_service.jobs WHERE id = %s FOR UPDATE",
                                     (job["id"],))
            if not await cur.fetchone():
                raise RuntimeError("提交时记忆任务不存在")
            await store.apply_operations(conn, plan.operations, changes=changes)
            payload = job["payload"]
            fingerprints = {
                item["data"]["id"]: message_fingerprint(item)
                for item in payload["messages"]
            }
            await conn.execute("""
                INSERT INTO memory_service.results
                    (job_id, job_key, character_name, thread_id,
                     through_message_id, through_fingerprint, trim)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (job["id"], job["job_key"], job["character_name"], job["thread_id"],
                  payload["through_message_id"], message_fingerprint(payload["messages"][-1]),
                  Jsonb({"remove_ids": plan.remove_ids, "fingerprints": fingerprints})))
            await conn.execute("DELETE FROM memory_service.jobs WHERE id = %s", (job["id"],))
        if changes:
            store.log_changes(changes, source="worker", job_id=job["id"], thread_id=job["thread_id"])
        return True

    async def fail(self, conn, job: dict, error: Exception, max_attempts: int) -> None:
        # 只计入已捕获的失败；硬崩溃不消耗重试次数，重启直接从仍在表中的任务计算。
        attempts = job["attempts"] + 1
        status = "failed" if attempts >= max_attempts else "pending"
        delay = min(300, 2 ** min(attempts, 8))
        await conn.execute("""
            UPDATE memory_service.jobs SET attempts = %s, status = %s, last_error = %s,
                next_attempt_at = now() + (%s * interval '1 second') WHERE id = %s
        """, (attempts, status, type(error).__name__, delay, job["id"]))
        log_failure(logger, "记忆任务失败，队列保留", error, job_id=job["id"],
                    character=job["character_name"], thread_id=job["thread_id"],
                    attempts=attempts, status=status, retry_delay_seconds=delay if status == "pending" else None)

    async def results_after(self, character_name: str, thread_id: str, after_id: int) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                SELECT * FROM memory_service.results
                WHERE character_name = %s AND thread_id = %s AND job_id > %s ORDER BY job_id
            """, (character_name, thread_id, after_id))
            return [dict(row, **row["trim"]) for row in await cur.fetchall()]

    async def acknowledge(self, character_name: str, thread_id: str, through_id: int) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("""
                UPDATE memory_service.results SET acknowledged_at = now()
                WHERE character_name = %s AND thread_id = %s AND job_id <= %s
                  AND acknowledged_at IS NULL
            """, (character_name, thread_id, through_id))

    async def job_status(self, job_id: int) -> str | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT status FROM memory_service.jobs WHERE id = %s", (job_id,))
            row = await cur.fetchone()
            return row["status"] if row else None

    async def retry(self, job_id: int) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                UPDATE memory_service.jobs SET status = 'pending', attempts = 0,
                    last_error = NULL, next_attempt_at = now()
                WHERE id = %s AND status = 'failed' RETURNING id
            """, (job_id,))
            retried = await cur.fetchone() is not None
        if retried:
            log_event(logger, "记忆任务手动重试", job_id=job_id)
        return retried
