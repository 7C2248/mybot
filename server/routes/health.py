from fastapi import APIRouter, Request, Response

from server import __version__
from server.classes.api import HealthStatus, ReadyStatus
from server.services.health import readiness

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthStatus)
async def health():
    return HealthStatus(version=__version__)


@router.get("/ready", response_model=ReadyStatus, responses={503: {"model": ReadyStatus}})
async def ready(request: Request, response: Response):
    result = await readiness(request.app.state.database, configured=bool(request.app.state.settings.db_url))
    result.capabilities.chat = result.database == "connected" and request.app.state.runtime.ready and request.app.state.models.active is not None
    result.capabilities.speech = result.capabilities.chat
    if result.status != "ready":
        response.status_code = 503
    return result
