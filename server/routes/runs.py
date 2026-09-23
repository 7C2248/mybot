import asyncio
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from server.classes.api import ServiceError
from server.classes.runs import AcceptedRun, RequestKey, RunEvent, RunSnapshot, SubmitRun
from server.repositories.runs import TERMINAL
from server.routes.threads import repository

router = APIRouter(tags=["runs"])


def require_runtime(request):
    runtime = request.app.state.runtime
    if not runtime.ready:
        raise ServiceError("runtime_unavailable", "运行服务尚未就绪。", 503)
    request.app.state.models.snapshot()


@router.post("/threads/{thread_id}/runs", response_model=AcceptedRun, status_code=202)
async def submit(thread_id: UUID, body: SubmitRun, request: Request):
    require_runtime(request)
    repo = repository(request)
    thread = await repo.get_thread(thread_id)
    request.app.state.catalog.get(thread["character_id"])
    run = await repo.submit(thread_id, body.text, body.client_request_id)
    return {"run_id": run["id"], "status": run["status"]}


@router.post("/runs/{run_id}/retry", response_model=AcceptedRun, status_code=202)
async def retry(run_id: UUID, body: RequestKey, request: Request):
    require_runtime(request)
    run = await repository(request).retry(run_id, body.client_request_id)
    return {"run_id": run["id"], "status": run["status"]}


@router.get("/runs/{run_id}", response_model=RunSnapshot)
async def snapshot(run_id: UUID, request: Request):
    return await repository(request).snapshot(run_id)


@router.get("/runs/{run_id}/events")
async def events(run_id: UUID, request: Request,
                 after: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
                 last_event_id: Annotated[str | None, Header(max_length=30)] = None):
    if last_event_id is not None:
        if not last_event_id.isascii() or not last_event_id.isdigit():
            raise ServiceError("invalid_event_id", "事件序号无效。")
        after = max(after, int(last_event_id))
        if after > 9223372036854775807:
            raise ServiceError("invalid_event_id", "事件序号无效。")
    repo = repository(request)
    initial = await repo.snapshot(run_id)
    if after > initial["last_event_sequence"]:
        raise ServiceError("invalid_event_id", "事件序号超过当前运行。")

    async def stream():
        sequence = after
        idle = 0
        while not await request.is_disconnected():
            rows = await repo.events(run_id, sequence)
            for row in rows:
                event = RunEvent.model_validate(row)
                sequence = event.sequence
                data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
                yield f"id: {sequence}\nevent: {event.type}\ndata: {data}\n\n"
            current = await repo.snapshot(run_id)
            # Re-read the terminal sequence after fetching events to close the completion race.
            if current["status"] in TERMINAL and sequence >= current["last_event_sequence"]:
                return
            if rows:
                idle = 0
                continue
            idle += 1
            if idle % 40 == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
