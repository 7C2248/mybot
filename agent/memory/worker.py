"""独立单任务消费者：python -m agent.memory.worker；退出/崩溃不删除队列任务。"""

import argparse
import asyncio
from time import perf_counter

# Windows 的数据库事件循环策略必须在 asyncio.run 创建循环之前安装。
import core.db

from agent.memory.jobs import MemoryJobRepository
from agent.memory.processor import process_memory_snapshot
from agent.memory.store import AsyncPostgresCharacterMemoryStore
from utils.daily_logger import get_logger, log_event, log_failure, logging_context

logger = get_logger("memory.worker")


class MemoryWorker:
    def __init__(self, jobs, *, store_factory=None, processor=process_memory_snapshot, max_attempts=5):
        self.jobs = jobs
        self.store_factory = store_factory or AsyncPostgresCharacterMemoryStore.create
        self.processor = processor
        self.max_attempts = max_attempts
        self.active_job = None

    async def run_once(self) -> bool:
        async with self.jobs.worker_session() as conn:
            if conn is None:
                return False
            job = await self.jobs.next_job(conn)
            if job is None:
                return False
            from agent.memory.policy import memory_permitted
            if not await memory_permitted(conn, job['payload']):
                await conn.execute('DELETE FROM memory_service.jobs WHERE id = %s', (job['id'],))
                log_event(logger, "记忆任务已撤销", job_id=job["id"], thread_id=job["thread_id"],
                          character=job["character_name"], stage="before_processing")
                return True
            with logging_context(job_id=job["id"], thread_id=job["thread_id"], character=job["character_name"]):
                await self._process(conn, job)
            return True

    async def _process(self, conn, job):
        started = perf_counter()
        try:
            self.active_job = {'id': job['id'], 'thread_id': job['thread_id'], 'character_name': job['character_name']}
            log_event(logger, "记忆任务开始", attempt=job["attempts"] + 1)
            store = await self.store_factory(self.jobs.pool, job["character_name"])
            # 锁覆盖检索/计算/提交，避免另一会话或常规 CRUD 改动同一角色库。
            # 这是会话锁；计算期间没有写事务。
            async with store.write_session(conn):
                plan = await self.processor(job["payload"], store)
                committed = await self.jobs.finish(conn, job, store, plan)
            if committed is False:
                log_event(logger, "记忆任务已撤销", stage="before_commit")
            else:
                log_event(logger, "记忆任务完成", operations=len(plan.operations), trim=len(plan.remove_ids),
                          duration_ms=round((perf_counter() - started) * 1000, 2))
        except asyncio.CancelledError:
            log_event(logger, "记忆任务中断，等待恢复")
            raise
        except Exception as exc:
            # CancelledError/进程终止不在此捕获，任务原样留在队列。
            await self.jobs.fail(conn, job, exc, self.max_attempts)
        finally:
            self.active_job = None


async def run_worker(*, once=False, poll_interval=1.0, max_attempts=5, retry=None):
    await core.db.init_db(checkpoints=False)
    try:
        jobs = await MemoryJobRepository.create(core.db.pool)
        if retry is not None and not await jobs.retry(retry):
            raise ValueError("指定任务不存在或不处于 failed 状态")
        worker = MemoryWorker(jobs, max_attempts=max_attempts)
        while True:
            try:
                processed = await worker.run_once()
            except Exception as exc:
                log_failure(logger, "记忆队列数据库操作失败", exc)
                if once:
                    raise
                processed = False
            if once:
                break
            if not processed:
                await asyncio.sleep(poll_interval)
    finally:
        await core.db.close_db()


def parse_args():
    parser = argparse.ArgumentParser(description="Run the durable single-task memory worker.")
    parser.add_argument("--once", action="store_true", help="尝试处理一个可执行任务后退出")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--retry", type=int, help="将指定 failed 任务重新置为 pending")
    args = parser.parse_args()
    if args.poll_interval <= 0 or args.max_attempts < 1:
        parser.error("poll-interval 必须大于零，max-attempts 必须至少为 1")
    return args


if __name__ == "__main__":
    options = parse_args()
    try:
        asyncio.run(run_worker(once=options.once, poll_interval=options.poll_interval,
                               max_attempts=options.max_attempts, retry=options.retry))
    except KeyboardInterrupt:
        pass
