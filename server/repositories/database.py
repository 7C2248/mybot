"""Application-owned pool and transactional, checksum-verified service migrations."""

import hashlib
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier
from psycopg_pool import AsyncConnectionPool

from server.config import ServiceSettings

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


async def apply_migrations(pool, directory: Path = MIGRATIONS) -> str:
    files = sorted(directory.glob("[0-9]*.sql"))
    if not files:
        raise RuntimeError("Service migrations are missing")
    # Normalize CRLF/LF before hashing, so a Git checkout on another OS keeps the checksum.
    sources = {path.stem: path.read_text(encoding="utf-8") for path in files}
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('mybot-ui-migrations', 0))")
            await conn.execute("CREATE SCHEMA IF NOT EXISTS mybot_ui")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS mybot_ui.schema_migrations (
                    version TEXT PRIMARY KEY, checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            applied = {row["version"]: row["checksum"] for row in
                       await (await conn.execute("SELECT version, checksum FROM mybot_ui.schema_migrations")).fetchall()}
            if applied.keys() - sources.keys():
                raise RuntimeError("Database service schema is newer than this server")
            for version, source in sources.items():
                checksum = hashlib.sha256(source.encode("utf-8")).hexdigest()
                if version in applied:
                    if applied[version] != checksum:
                        raise RuntimeError("An applied service migration has changed")
                    continue
                await conn.execute(source)
                await conn.execute("INSERT INTO mybot_ui.schema_migrations (version, checksum) VALUES (%s, %s)",
                                   (version, checksum))
    return files[-1].stem


class Database:
    def __init__(self, settings: ServiceSettings):
        self.settings = settings
        self.pool = None
        self.schema_version = None

    async def open(self):
        if self.pool is not None or not self.settings.db_url:
            return
        async def configure(conn):
            await conn.execute(SQL("SET search_path TO {}, public").format(Identifier(self.settings.memory_schema)))

        self.pool = AsyncConnectionPool(
            self.settings.db_url, min_size=1, max_size=5, open=False,
            timeout=self.settings.db_timeout, check=AsyncConnectionPool.check_connection,
            configure=configure,
            kwargs={"autocommit": True, "row_factory": dict_row,
                    "connect_timeout": max(1, int(self.settings.db_timeout)),
                    "options": "-c statement_timeout=5000 -c lock_timeout=5000"},
        )
        try:
            await self.pool.open(wait=True, timeout=self.settings.db_timeout)
            self.schema_version = await apply_migrations(self.pool)
        except BaseException:
            await self.close()
            raise

    async def ping(self) -> bool:
        if self.pool is None or self.schema_version is None:
            return False
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT version FROM mybot_ui.schema_migrations WHERE version = %s", (self.schema_version,)
            )).fetchone()
            return row is not None

    async def close(self):
        try:
            if self.pool is not None:
                await self.pool.close()
        finally:
            self.pool = None
            self.schema_version = None
