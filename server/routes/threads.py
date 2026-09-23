from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from server.classes.api import ServiceError
from server.classes.runs import CreateThread, CheckpointSync, CheckpointSyncResult, MessagePage, Thread, ThreadPage, ThreadVersion, UpdateThread, MemoryStatus
from server.repositories.runs import RunRepository
from server.repositories.threads import ThreadRepository

router = APIRouter(prefix="/threads", tags=["threads"])


def repository(request: Request):
    database = request.app.state.database
    if database.pool is None:
        raise ServiceError("database_unavailable", "数据库尚未就绪。", 503)
    return ThreadRepository(database.pool)


@router.get("", response_model=ThreadPage)
async def threads(request: Request, cursor: Annotated[str | None, Query(max_length=2048)] = None,
                  limit: Annotated[int, Query(ge=1, le=100)] = 30, deleted: bool = False):
    return await repository(request).list_threads(cursor=cursor, limit=limit, deleted=deleted)


@router.post("", response_model=Thread, status_code=201)
async def create_thread(body: CreateThread, request: Request):
    request.app.state.catalog.get(body.character_id)
    return await repository(request).create_thread(**body.model_dump())


@router.get('/{thread_id}', response_model=Thread)
async def detail(thread_id: UUID, request: Request):
    return await repository(request).get_thread(thread_id)


@router.patch('/{thread_id}', response_model=Thread)
async def update(thread_id: UUID, body: UpdateThread, request: Request):
    changes = body.model_dump(exclude_none=True, exclude={'expected_version'})
    if not changes:
        raise ServiceError('empty_update', '没有需要修改的会话设置。')
    return await repository(request).update(thread_id, body.expected_version, changes)


@router.delete('/{thread_id}', response_model=Thread)
async def delete(thread_id: UUID, body: ThreadVersion, request: Request):
    return await repository(request).trash(thread_id, body.expected_version)


@router.post('/{thread_id}/restore', response_model=Thread)
async def restore(thread_id: UUID, body: ThreadVersion, request: Request):
    return await repository(request).trash(thread_id, body.expected_version, restore=True)


@router.delete('/{thread_id}/purge')
async def purge(thread_id: UUID, body: ThreadVersion, request: Request):
    await repository(request).purge(thread_id, body.expected_version)
    return {'deleted': True}


@router.get("/{thread_id}/messages", response_model=MessagePage)
async def messages(thread_id: UUID, request: Request, before: Annotated[str | None, Query(max_length=2048)] = None,
                   limit: Annotated[int, Query(ge=1, le=100)] = 30):
    return await repository(request).messages(thread_id, before=before, limit=limit)


@router.get("/{thread_id}/state")
async def state(thread_id: UUID, request: Request):
    thread = await repository(request).get_thread(thread_id)
    return thread["state"]


@router.post('/{thread_id}/checkpoint-sync', response_model=CheckpointSyncResult)
async def checkpoint_sync(thread_id: UUID, body: CheckpointSync, request: Request):
    return await request.app.state.checkpoint_sync.sync(thread_id, body)


@router.get('/{thread_id}/memory-status', response_model=MemoryStatus)
async def memory_status(thread_id: UUID, request: Request):
    memory = getattr(request.app.state.runtime, 'memory', None)
    return await repository(request).memory_status(thread_id, getattr(memory, 'active_job', None))
