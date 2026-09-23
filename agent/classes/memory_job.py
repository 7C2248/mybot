"""后台记忆计算产物；不包含数据库连接或可变图状态。"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PreparedMemory:
    text: str
    importance: int | None
    event_date: str | None
    keywords: str | None
    chunks: list[tuple[str, Any]]


@dataclass
class MemoryOperation:
    name: str
    memory_id: int | None = None
    prepared: PreparedMemory | None = None


@dataclass
class MemoryPlan:
    operations: list[MemoryOperation] = field(default_factory=list)
    remove_ids: list[str] = field(default_factory=list)
