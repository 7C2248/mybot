from typing import Annotated

from fastapi import APIRouter, Path, Query, Request

from server.classes.api import MemoryPage, MemoryRecord, ServiceError
from server.repositories.memories import MemoryRepository

router = APIRouter(prefix="/characters/{character_id}/memories", tags=["memories"])


def _repository(request: Request, character_id: str) -> MemoryRepository:
    catalog = request.app.state.catalog
    catalog.get(character_id)
    database = request.app.state.database
    if database.pool is None:
        raise ServiceError("database_unavailable", "记忆数据库尚未就绪。", 503)
    return MemoryRepository(database.pool, catalog, request.app.state.settings.memory_schema)


@router.get("", response_model=MemoryPage)
async def memories(character_id: str, request: Request,
                   query: Annotated[str, Query(max_length=1000)] = "",
                   cursor: Annotated[str | None, Query(max_length=2048)] = None,
                   limit: Annotated[int, Query(ge=1, le=100)] = 20):
    return await _repository(request, character_id).list(character_id, query=query, cursor=cursor, limit=limit)


@router.get("/{memory_id}", response_model=MemoryRecord)
async def memory(character_id: str, request: Request,
                 memory_id: Annotated[int, Path(ge=1, le=9223372036854775807)]):
    return await _repository(request, character_id).get(character_id, memory_id)
