import secrets
from typing import Annotated

from fastapi import APIRouter, Header, Request

from server.classes.api import ServiceError

router = APIRouter(prefix="/service", tags=["service"])


@router.get("")
async def status(request: Request):
    memory = getattr(request.app.state.runtime, 'memory', None)
    return {"managed": bool(request.app.state.settings.owner_token),
            "runtime_ready": request.app.state.runtime.ready,
            "runtime_error": request.app.state.runtime.error_code,
            'memory_runtime_ready': getattr(memory, 'ready', False),
            'memory_runtime_error': getattr(memory, 'error_code', None),
            "startup_mode": "desktop" if request.app.state.settings.owner_token else "manual"}


@router.post("/shutdown", status_code=202)
async def shutdown(request: Request, x_mybot_owner: Annotated[str | None, Header()] = None):
    owner = request.app.state.settings.owner_token
    if not owner or not x_mybot_owner or not secrets.compare_digest(owner, x_mybot_owner):
        raise ServiceError("owner_required", "只能关闭由当前桌面程序启动的服务。", 403)
    callback = getattr(request.app.state, "shutdown_callback", None)
    if callback is None:
        raise ServiceError("shutdown_unavailable", "当前启动方式不支持托管退出。", 409)
    callback()
    return {"status": "stopping"}
