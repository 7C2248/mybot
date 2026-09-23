from typing import Literal

from pydantic import Field, field_validator

from server.classes.runs import RequestModel

PUBLIC_NODES = {"main", "participant_state", "memory_query", "memory_summary", "chunking", "tts"}


class ProfileWrite(RequestModel):
    expected_version: str = Field(min_length=64, max_length=64)
    text: str = Field(min_length=1, max_length=200000)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Profile must not be blank")
        return value


class NodeModelSettings(RequestModel):
    provider: Literal["deepseek", "moonshot", "llama_cpp"]
    model: str = Field(min_length=1, max_length=200)
    thinking: Literal["enabled", "disabled"] = "enabled"
    reasoning_effort: Literal["none", "low", "medium", "high", "max"] = "max"


class ModelSettingsWrite(RequestModel):
    expected_version: str = Field(min_length=64, max_length=64)
    nodes: dict[str, NodeModelSettings] = Field(min_length=1)

    @field_validator("nodes")
    @classmethod
    def public_nodes_only(cls, value):
        if value.keys() - PUBLIC_NODES:
            raise ValueError("Unknown or protected model node")
        return value


class ApplyModelSettings(RequestModel):
    expected_version: str = Field(min_length=64, max_length=64)
