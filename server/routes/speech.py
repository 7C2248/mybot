from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from server.classes.api import ServiceError
from server.classes.runs import RequestKey, SpeechJob
from server.repositories.speech import SpeechRepository
from server.routes.runs import require_runtime
from server.routes.threads import repository
from server.services.speech import audio_path

router = APIRouter(tags=["speech"])


def _repo(request):
    repository(request)  # Consistent database availability check.
    return SpeechRepository(request.app.state.database.pool)


@router.post("/messages/{message_id}/speech", response_model=SpeechJob, status_code=202)
async def synthesize(message_id: UUID, body: RequestKey, request: Request):
    require_runtime(request)
    return await _repo(request).enqueue(message_id, body.client_request_id)


@router.get("/speech/{job_id}", response_model=SpeechJob)
async def snapshot(job_id: UUID, request: Request):
    return await _repo(request).get(job_id)


@router.get("/audio/resources/{resource_id}", response_class=FileResponse)
async def resource(resource_id: UUID, request: Request):
    await _repo(request).resource(resource_id)
    path = audio_path(request.app.state.settings.audio_root, resource_id)
    if not path.is_file():
        raise ServiceError("resource_not_found", "音频资源不存在。", 404)
    return FileResponse(path, media_type="audio/wav", headers={"X-Content-Type-Options": "nosniff"})
