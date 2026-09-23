"""Memory consumes its durable queue independently of conversation execution."""
import asyncio
from concurrent.futures import Future
import threading

from server.repositories.database import Database
from utils.daily_logger import get_logger, log_event, log_failure

logger = get_logger('memory_runtime')


class MemoryRuntime:
    def __init__(self, settings, models, *, worker_factory=None):
        self.settings, self.models = settings, models
        self.worker_factory = worker_factory
        self.ready = False
        self.error_code = None
        self.started = Future()
        self.stopping = threading.Event()
        self.thread = self.loop = self.task = self.worker = None

    @property
    def active_job(self):
        return getattr(self.worker, 'active_job', None)

    async def start(self):
        if not self.settings.db_url or not self.settings.enable_runs or not self.settings.memory_worker:
            return
        self.thread = threading.Thread(target=self._thread_main, name='mybot-memory', daemon=True)
        self.thread.start()
        await asyncio.wrap_future(self.started)

    def _thread_main(self):
        from server.__main__ import create_event_loop
        try:
            with asyncio.Runner(loop_factory=create_event_loop) as runner:
                runner.run(self._serve())
        except BaseException as error:
            self.error_code = 'memory_runtime_unavailable'
            log_failure(logger, '后台记忆执行器异常停止', error)
            if not self.started.done():
                self.started.set_exception(RuntimeError('Memory runtime initialization failed'))
        finally:
            self.ready = False

    async def _serve(self):
        from config.model_config import model_config_scope
        from agent.memory.jobs import MemoryJobRepository
        from agent.memory.worker import MemoryWorker
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        database = Database(self.settings)
        try:
            await database.open()
            jobs = await MemoryJobRepository.create(database.pool)
            self.worker = (self.worker_factory or MemoryWorker)(jobs)
            self.ready = True
            self.started.set_result(None)
            while not self.stopping.is_set():
                worked = False
                # This protects settings activation only. The conversation gate is independent.
                if self.models.memory_gate.acquire(blocking=False):
                    try:
                        if self.models.active is not None:
                            data, _ = self.models.snapshot()
                            with model_config_scope(data):
                                worked = await self.worker.run_once()
                            if self.error_code is not None:
                                log_event(logger, '后台记忆队列已恢复')
                            self.error_code = None
                    except Exception as error:
                        if self.error_code is None:
                            log_failure(logger, '后台记忆队列不可用', error)
                        self.error_code = 'memory_runtime_unavailable'
                    finally:
                        self.models.memory_gate.release()
                if not worked:
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        finally:
            self.ready = False
            await database.close()

    async def close(self):
        self.ready = False
        self.stopping.set()
        if self.thread is not None and self.thread.is_alive():
            if self.loop is not None and not self.loop.is_closed() and self.task is not None:
                self.loop.call_soon_threadsafe(self.task.cancel)
            await asyncio.to_thread(self.thread.join)
