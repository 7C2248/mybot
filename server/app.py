"""FastAPI application factory with explicitly owned resources."""

import asyncio
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import Error as PostgresError
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from server import __version__
from server.classes.api import ErrorResponse, ServiceError
from server.config import ServiceSettings
from server.repositories.database import Database
from server.routes import characters, health, memories, runs, threads, speech, service, legacy, settings as settings_routes
from server.services.access import LocalAccessMiddleware
from server.services.characters import CharacterCatalog
from server.services.checkpoint_sync import CheckpointSyncService
from server.services.model_settings import ModelSettingsService
from server.services.runtime import RunRuntime
from server.services.legacy import LegacyService
from utils.daily_logger import get_logger, log_event, log_failure

logger = get_logger("server")


def _error(code: str, message: str, status: int, details=None):
    return JSONResponse({"error": {"code": code, "message": message, **({"details": details} if details else {})}}, status_code=status)


def create_app(settings: ServiceSettings | None = None, *, database_factory=Database, runtime_factory=RunRuntime) -> FastAPI:
    settings = settings or ServiceSettings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = database_factory(settings)
        app.state.settings = settings
        app.state.database = database
        app.state.catalog = CharacterCatalog(settings.character_root)
        gate = threading.Lock()
        app.state.models = ModelSettingsService(settings, gate)
        app.state.runtime = runtime_factory(settings, app.state.catalog, app.state.models, gate)
        app.state.legacy = LegacyService(database, app.state.catalog, settings.audio_root)
        app.state.checkpoint_sync = CheckpointSyncService(database)
        log_event(logger, "后端启动", port=settings.port, runs_enabled=settings.enable_runs,
                  memory_enabled=settings.memory_worker)
        try:
            await asyncio.to_thread(app.state.catalog.load)
            await database.open()
            await app.state.runtime.start()
            await app.state.legacy.start()
            log_event(logger, "后端就绪", runtime_ready=app.state.runtime.ready,
                      memory_ready=bool(getattr(getattr(app.state.runtime, "memory", None), "ready", False)))
            yield
        except Exception as exc:
            log_failure(logger, "后端生命周期异常", exc)
            raise
        finally:
            log_event(logger, "后端停止中")
            try:
                await app.state.legacy.close()
                await app.state.runtime.close()
            finally:
                await database.close()
                log_event(logger, "后端已停止")

    app = FastAPI(
        title="mybot local API", version=__version__, lifespan=lifespan,
        description="本机角色对话、完整历史、运行事件、档案、模型设置与语音服务。",
        responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                   422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    app.add_middleware(LocalAccessMiddleware, allowed_origins=settings.allowed_origins)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins),
                       allow_methods=["GET", "HEAD", "POST", "PUT", 'PATCH', 'DELETE', "OPTIONS"],
                       allow_headers=["Content-Type", "X-Mybot-Client", "Last-Event-ID", "X-Mybot-Owner"],
                       allow_credentials=False)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"], www_redirect=False)

    @app.exception_handler(ServiceError)
    async def service_error(_request, exc):
        log_event(logger, "业务请求失败", code=exc.code, status=exc.status_code)
        return _error(exc.code, exc.message, exc.status_code, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        # Pydantic's default error includes submitted input; keep the public error bounded.
        return _error("invalid_request", "请求参数无效，请检查类型、分页范围和长度。", 422)

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return _error("not_found" if exc.status_code == 404 else "http_error",
                      "接口不存在。" if exc.status_code == 404 else "请求无法处理。", exc.status_code)

    async def database_error(_request, exc):
        log_failure(logger, "数据库请求失败", exc)
        return _error("database_unavailable", "数据库暂时不可用，请稍后重试。", 503)

    app.add_exception_handler(PostgresError, database_error)
    app.add_exception_handler(PoolTimeout, database_error)

    @app.exception_handler(Exception)
    async def unexpected_error(_request, exc):
        log_failure(logger, "未处理的服务异常", exc)
        return _error("internal_error", "服务暂时无法处理此请求。", 500)

    for router in (health.router, characters.router, memories.router, settings_routes.router,
                   threads.router, runs.router, speech.router, service.router, legacy.router):
        app.include_router(router, prefix="/api")
    return app
