from typing import Annotated
from fastapi import APIRouter, Request, Query
from server.classes.runs import ImportThread, ResolveCLI

router = APIRouter(tags=['cli'])


@router.get('/legacy/threads')
async def discover(request: Request, cursor: Annotated[str | None, Query(max_length=255)] = None):
    return await request.app.state.legacy.discover(cursor=cursor)


@router.post('/legacy/threads/{source_id}/import', status_code=202)
async def import_thread(source_id: str, body: ImportThread, request: Request):
    return await request.app.state.legacy.request_import(source_id, body.character_id, body.title)


@router.get('/legacy/threads/{source_id}')
async def import_status(source_id: str, request: Request):
    return await request.app.state.legacy.status(source_id)


@router.get('/legacy/threads/{source_id}/messages')
async def preview(source_id: str, request: Request):
    return await request.app.state.legacy.preview(source_id)


@router.post('/cli/resolve')
async def resolve(body: ResolveCLI, request: Request):
    return await request.app.state.legacy.resolve(body.source_id, body.character_id, body.title,
                                                body.memory_retrieval_enabled, body.memory_storage_enabled)
