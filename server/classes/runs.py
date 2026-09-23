from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateThread(RequestModel):
    character_id: str = Field(min_length=1, max_length=255)
    title: str = Field(default="新对话", min_length=1, max_length=200)
    memory_retrieval_enabled: bool = False
    memory_storage_enabled: bool = False
    source: Literal['desktop', 'cli'] = 'desktop'


class ThreadVersion(RequestModel):
    expected_version: int = Field(ge=1)


class UpdateThread(ThreadVersion):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    memory_retrieval_enabled: bool | None = None
    memory_storage_enabled: bool | None = None


class CheckpointSync(ThreadVersion):
    dry_run: bool = False
    checkpoint_id: str | None = Field(default=None, max_length=200)


class ImportThread(RequestModel):
    character_id: str = Field(min_length=1, max_length=255)
    title: str = Field(default='CLI 历史', min_length=1, max_length=200)


class ResolveCLI(CreateThread):
    source_id: str = Field(min_length=1, max_length=255)


class RequestKey(RequestModel):
    client_request_id: str = Field(min_length=1, max_length=200)

    @field_validator("client_request_id")
    @classmethod
    def nonblank_key(cls, value):
        if not value.strip():
            raise ValueError("Empty request key")
        return value


class SubmitRun(RequestKey):
    text: str = Field(min_length=1, max_length=50000)

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value):
        if not value.strip():
            raise ValueError("Empty message")
        return value


class Message(BaseModel):
    id: UUID
    thread_id: UUID
    run_id: UUID | None
    sequence: int
    role: Literal["user", "assistant"]
    text: str
    created_at: datetime
    source: Literal['run', 'legacy'] = 'run'
    timestamp_estimated: bool = False


RunStatus = Literal["queued", "running", "completed", "completed_with_warnings", "failed", "interrupted"]


class RunSummary(BaseModel):
    id: UUID
    thread_id: UUID
    user_message_id: UUID
    client_request_id: str
    status: RunStatus
    phase: str
    retry_of: UUID | None = None
    error_code: str | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model_version: str | None = None
    profile_version: str | None = None
    memory_retrieval_enabled: bool = True
    memory_storage_enabled: bool = True
    memory_policy_version: int = 1


class RunSnapshot(RunSummary):
    messages: list[Message]
    last_event_sequence: int


class AcceptedRun(BaseModel):
    run_id: UUID
    status: RunStatus


class Thread(BaseModel):
    id: UUID
    character_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    current_run: RunSummary | None = None
    latest_run: RunSummary | None = None
    version: int = 1
    memory_policy_version: int = 1
    memory_retrieval_enabled: bool = False
    memory_storage_enabled: bool = False
    deleted_at: datetime | None = None
    source: Literal['desktop', 'cli', 'legacy'] = 'desktop'
    history_notice: str | None = None


class ThreadPage(BaseModel):
    items: list[Thread]
    next_cursor: str | None = None


class MemoryStatus(BaseModel):
    status: Literal['disabled', 'idle', 'pending', 'running', 'failed', 'completed']
    job_id: int | None = None


class MessagePage(BaseModel):
    items: list[Message]
    next_cursor: str | None = None


class CheckpointSyncResult(BaseModel):
    dry_run: bool
    checkpoint_id: str | None = None
    latest_message_id: str | None = None
    latest_message_text: str | None = None
    matched_message_id: str | None = None
    delete_count: int
    delete_preview: list[Message] = Field(default_factory=list)
    preview_truncated: bool = False
    run_count: int = 0
    version: int
    state: dict = Field(default_factory=dict)


class RunEvent(BaseModel):
    run_id: UUID
    sequence: int
    type: Literal["run.started", "phase", "message.committed", "state.updated", "memory.retrieved",
                  "run.completed", "run.failed"]
    payload: dict
    created_at: datetime


class SpeechJob(BaseModel):
    id: UUID
    message_id: UUID
    status: Literal["queued", "running", "completed", "failed", "interrupted"]
    resource_id: UUID | None = None
    resource_url: str | None = None
    error_code: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
