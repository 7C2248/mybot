"""Conversation and speech execution; memory runs in its own worker loop."""

import asyncio
from concurrent.futures import Future
import threading
from time import perf_counter

from server.repositories.database import Database
from server.repositories.runs import RunRepository
from server.repositories.speech import SpeechRepository
from utils.daily_logger import get_logger, log_event, log_failure, logging_context

logger = get_logger("runtime")


class RunRuntime:
    def __init__(self, settings, catalog, models, gate, *, adapter_factory=None, speech_factory=None, memory_worker_factory=None):
        self.settings, self.catalog, self.models, self.gate = settings, catalog, models, gate
        self.adapter_factory, self.speech_factory = adapter_factory, speech_factory
        self.ready = False
        self.error_code = None
        self.stopping = threading.Event()
        self.started = Future()
        self.thread = None
        self.loop = None
        self.task = None
        from server.services.memory_runtime import MemoryRuntime
        self.memory = MemoryRuntime(settings, models, worker_factory=memory_worker_factory)

    async def start(self):
        if not self.settings.db_url or not self.settings.enable_runs:
            return
        self.thread = threading.Thread(target=self._thread_main, name="mybot-runs", daemon=True)
        self.thread.start()
        await asyncio.wrap_future(self.started)
        try:
            await self.memory.start()
        except Exception as error:
            # An unavailable memory consumer must not take the conversation API offline.
            log_failure(logger, "后台记忆暂不可用，对话服务继续运行", error)
            await self.memory.close()

    def _thread_main(self):
        from server.__main__ import create_event_loop
        try:
            with asyncio.Runner(loop_factory=create_event_loop) as runner:
                runner.run(self._serve())
        except BaseException as exc:
            self.ready = False
            self.error_code = "runtime_unavailable"
            log_failure(logger, "对话执行器异常停止", exc)
            if not self.started.done():
                self.started.set_exception(RuntimeError("Agent runtime initialization failed"))

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        database = Database(self.settings)
        try:
            await database.open()
            async with database.pool.connection() as conn:
                locked = await (await conn.execute("SELECT pg_try_advisory_lock(hashtextextended('mybot-ui-runtime', 0)) AS locked")).fetchone()
                if not locked["locked"]:
                    raise RuntimeError("Another service owns the run worker")
                try:
                    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
                    from server.services.agent import AgentAdapter
                    from server.services.speech import SpeechService
                    # Checkpoint writes share the ownership connection too. A severed
                    # session cannot keep writing checkpoints through a fresh pool lease.
                    checkpointer = AsyncPostgresSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=None))
                    await checkpointer.setup()
                    repo = RunRepository(database.pool, connection=conn, connection_lock=checkpointer.lock)
                    speech_repo = SpeechRepository(database.pool, connection=conn, connection_lock=checkpointer.lock)
                    adapter = (self.adapter_factory or AgentAdapter)(database.pool, checkpointer, self.catalog, self.models, stopping=self.stopping)
                    speech = (self.speech_factory or SpeechService)(self.settings.audio_root, self.catalog, self.models)
                    await speech_repo.recover()
                    for run in await repo.recovery_runs():
                        repaired = False
                        try:
                            await adapter.repair(run, repo)
                            repaired = True
                        except Exception as exc:
                            log_failure(logger, "启动时检查点恢复延后", exc, run_id=run["id"], thread_id=run["thread_id"])
                        await repo.finish(run["id"], error="service_interrupted", interrupted=True, needs_recovery=not repaired)
                    self.ready = True
                    self.started.set_result(None)
                    while not self.stopping.is_set():
                        worked = False
                        # Nonblocking acquisition avoids blocking this loop during model activation.
                        if self.gate.acquire(blocking=False):
                            try:
                                run = await repo.claim()
                                if run:
                                    worked = True
                                    await self._run_one(adapter, repo, run)
                                else:
                                    job = await speech_repo.claim()
                                    if job:
                                        worked = True
                                        started = perf_counter()
                                        log_event(logger, "语音任务开始", speech_job_id=job["id"],
                                                  message_id=job["message_id"], character=job["character_id"])
                                        try:
                                            await speech.execute(job)
                                            await speech_repo.finish(job["id"])
                                            log_event(logger, "语音任务完成", speech_job_id=job["id"],
                                                      duration_ms=round((perf_counter() - started) * 1000, 2))
                                        except asyncio.CancelledError:
                                            await speech_repo.recover()
                                            log_event(logger, "语音任务中断", speech_job_id=job["id"])
                                            raise
                                        except Exception as exc:
                                            log_failure(logger, "语音任务失败", exc, speech_job_id=job["id"])
                                            await speech_repo.finish(job["id"], error="speech_failed")
                            finally:
                                self.gate.release()
                        if not worked:
                            await asyncio.sleep(0.2)
                finally:
                    self.ready = False
                    if not conn.closed:
                        await conn.execute("SELECT pg_advisory_unlock(hashtextextended('mybot-ui-runtime', 0))")
        except asyncio.CancelledError:
            pass
        finally:
            await database.close()

    async def _run_one(self, adapter, repo, run):
        with logging_context(run_id=run["id"], thread_id=run["thread_id"], character=run["character_id"]):
            await self._execute_run(adapter, repo, run)

    async def _execute_run(self, adapter, repo, run):
        started = perf_counter()
        try:
            await adapter.execute(run, repo)
        except BaseException as exc:
            # This includes cancellation during shutdown. Never redo a committed reply.
            repaired = False
            try:
                current = await repo.recovery_context(run["id"])
                await adapter.repair(current, repo)
                repaired = True
            except Exception as repair_error:
                log_failure(logger, "检查点修复延后", repair_error)
            await repo.finish(run["id"], error="service_interrupted" if isinstance(exc, asyncio.CancelledError) else "agent_failed",
                              interrupted=isinstance(exc, asyncio.CancelledError), needs_recovery=not repaired)
            if isinstance(exc, asyncio.CancelledError):
                raise
            log_failure(logger, "对话执行失败", exc)
        finally:
            log_event(logger, "对话执行结束", duration_ms=round((perf_counter() - started) * 1000, 2))

    async def close(self):
        self.ready = False
        self.stopping.set()
        # Cancel memory before waiting for either thread's native inference to finish.
        memory_close = asyncio.create_task(self.memory.close())
        if self.thread is not None and self.thread.is_alive():
            if self.loop is not None and not self.loop.is_closed() and self.task is not None:
                self.loop.call_soon_threadsafe(self.task.cancel)
            # Native inference is not safely interruptible; allow its thread to finish.
            await asyncio.to_thread(self.thread.join)
        await memory_close
