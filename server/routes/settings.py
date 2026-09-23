import asyncio

from fastapi import APIRouter, Request

from server.classes.api import SettingsStatus
from server.classes.settings import ApplyModelSettings, ModelSettingsWrite
from server.services.health import readiness
from server.services.settings import read_settings

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=SettingsStatus)
async def settings(request: Request):
    state = await readiness(request.app.state.database, configured=bool(request.app.state.settings.db_url))
    result = await asyncio.to_thread(read_settings, request.app.state.settings,
                                     database_ready=state.database == "connected")
    result.read_only = False
    result.capabilities.chat = state.database == "connected" and request.app.state.runtime.ready and request.app.state.models.active is not None
    result.capabilities.speech = result.capabilities.chat
    return result


@router.get("/settings/models")
def models(request: Request):
    return request.app.state.models.get()


@router.put("/settings/models")
def save_models(body: ModelSettingsWrite, request: Request):
    return request.app.state.models.save(body)


@router.post("/settings/models/apply")
def apply_models(body: ApplyModelSettings, request: Request):
    return request.app.state.models.apply(body.expected_version)
