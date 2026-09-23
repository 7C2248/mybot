"""Service configuration, separate from model construction and CLI globals."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ServiceSettings:
    db_url: str = field(default="", repr=False)
    character_root: Path = PROJECT_ROOT / "Character"
    model_config: Path = PROJECT_ROOT / "config" / "models.yaml"
    environment: dict[str, str] = field(default_factory=dict, repr=False)
    port: int = 8765
    db_timeout: float = 5.0
    memory_schema: str = "public"
    enable_runs: bool = True
    memory_worker: bool = True
    audio_root: Path = PROJECT_ROOT / "data" / "audio"
    owner_token: str = field(default="", repr=False)

    def __post_init__(self):
        if not 1 <= self.port <= 65535:
            raise ValueError("MYBOT_API_PORT must be between 1 and 65535")
        if not 0 < self.db_timeout <= 60:
            raise ValueError("MYBOT_API_DB_TIMEOUT must be between 0 and 60 seconds")
        if not self.memory_schema or "\x00" in self.memory_schema:
            raise ValueError("MYBOT_MEMORY_SCHEMA is invalid")

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return (
            f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}",
            "http://127.0.0.1:5173", "http://localhost:5173",
            "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
        )

    @classmethod
    def from_environment(cls):
        values = {key: value for key, value in dotenv_values(PROJECT_ROOT / "config" / ".env").items()
                  if value is not None}
        values.update(os.environ)
        return cls(
            db_url=values.get("DB_URL", ""), environment=values,
            port=int(values.get("MYBOT_API_PORT", "8765")),
            db_timeout=float(values.get("MYBOT_API_DB_TIMEOUT", "5")),
            memory_schema=values.get("MYBOT_MEMORY_SCHEMA", "public"),
            enable_runs=values.get("MYBOT_API_RUNS", "1") == "1",
            memory_worker=values.get("MYBOT_API_MEMORY_WORKER", "1") == "1",
            owner_token=values.get("MYBOT_SERVICE_OWNER_TOKEN", ""),
        )
