"""participant_state 节点的结构化输出协议。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class UserStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    location: str | None = None
    mood: str | None = None
    body: str | None = None
    clothing: str | None = None


class CharacterStateUpdate(UserStateUpdate):
    hearing: Literal["same_room", "near", "far", "unknown"] | None = None


class ParticipantStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    character_state: CharacterStateUpdate
    user_state: UserStateUpdate
