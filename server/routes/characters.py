from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from server.classes.api import CharacterDetail, CharacterSummary
from server.classes.settings import ProfileWrite

router = APIRouter(tags=["characters"])


@router.get("/characters", response_model=list[CharacterSummary])
def characters(request: Request):
    return request.app.state.catalog.summaries()


@router.get("/characters/{character_id}", response_model=CharacterDetail)
def character(character_id: str, request: Request):
    return request.app.state.catalog.get(character_id)


@router.put("/characters/{character_id}/profiles/{language}", response_model=CharacterDetail)
def write_profile(character_id: str, language: str, body: ProfileWrite, request: Request):
    return request.app.state.catalog.write_profile(character_id, language, body.text, body.expected_version)


@router.get("/resources/{resource_id}", response_class=FileResponse, responses={
    200: {"content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}}},
})
def resource(resource_id: str, request: Request):
    item = request.app.state.catalog.resource(resource_id)
    return FileResponse(item.path, media_type=item.media_type,
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})
