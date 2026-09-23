from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, *, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class CharacterAsset(BaseModel):
    id: str
    name: str
    url: str


class CharacterSummary(BaseModel):
    id: str
    name: str
    languages: list[str]
    version: str
    assets: list[CharacterAsset]


class CharacterDetail(CharacterSummary):
    profiles: dict[str, str]


class MemoryRecord(BaseModel):
    id: str
    memory: str
    update_time: datetime | None = None
    importance: int | None = None
    event_date: date | None = None
    keywords: list[str] = Field(default_factory=list)


class MemoryPage(BaseModel):
    items: list[MemoryRecord]
    next_cursor: str | None = None


class HealthStatus(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["mybot"] = "mybot"
    version: str


class Capabilities(BaseModel):
    characters: bool = True
    resources: bool = True
    memories: bool = False
    chat: bool = False
    profile_write: bool = True
    model_settings: bool = True
    speech: bool = False


class ReadyStatus(BaseModel):
    status: Literal["ready", "not_ready"]
    database: Literal["connected", "not_configured", "unavailable"]
    schema_version: str | None = None
    capabilities: Capabilities


class ModelStatus(BaseModel):
    provider: Literal["deepseek", "moonshot", "llama_cpp", "unknown"]
    configured: bool
    credentials_configured: bool | None = None
    local_model_available: bool | None = None


class SettingsStatus(BaseModel):
    mode: Literal["service"] = "service"
    read_only: bool = True
    model_configuration: Literal["present", "missing", "invalid"]
    models: dict[str, ModelStatus]
    database_configured: bool
    capabilities: Capabilities
