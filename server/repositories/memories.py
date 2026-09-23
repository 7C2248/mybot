"""Browse existing parent rows without importing vector stores or loading models."""

import base64
import hashlib
import json
import re

from psycopg.errors import UndefinedTable
from psycopg.sql import SQL, Identifier

from server.classes.api import MemoryPage, MemoryRecord, ServiceError
from server.services.characters import CharacterCatalog


def _keywords(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # The existing store writes comma-separated text; also tolerate legacy JSON arrays.
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = None
        value = parsed if isinstance(parsed, list) else re.split(r"[,，;；\n]", value)
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))


def _record(row) -> MemoryRecord:
    return MemoryRecord(id=str(row["id"]), memory=row["memory"], update_time=row["update_time"],
                        importance=row["importance"], event_date=row["event_date"],
                        keywords=_keywords(row["keywords"]))


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _encode_cursor(character_id: str, query: str, before: int) -> str:
    value = json.dumps([1, character_id, _query_hash(query), before], separators=(",", ":"))
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None, character_id: str, query: str) -> int | None:
    if cursor is None:
        return None
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if (not isinstance(value, list) or len(value) != 4 or value[:3] != [1, character_id, _query_hash(query)]
                or type(value[3]) is not int or not 0 < value[3] <= 9223372036854775807):
            raise ValueError("Cursor scope mismatch")
        return value[3]
    except (ValueError, TypeError, UnicodeError):
        raise ServiceError("invalid_cursor", "分页游标无效，或不属于当前角色与查询。") from None


class MemoryRepository:
    def __init__(self, pool, catalog: CharacterCatalog, schema: str = "public"):
        self.pool = pool
        self.catalog = catalog
        self.schema = schema

    def _table(self, character_id: str):
        # Use the registered ID, never an arbitrary URL segment as a table name.
        character = self.catalog.get(character_id)
        return Identifier(self.schema, character.id)

    async def list(self, character_id: str, *, query: str = "", cursor: str | None = None,
                   limit: int = 20) -> MemoryPage:
        table = self._table(character_id)
        before = _decode_cursor(cursor, character_id, query)
        clauses = []
        params = []
        if before is not None:
            clauses.append(SQL("id < %s"))
            params.append(before)
        if query:
            clauses.append(SQL("memory ILIKE %s ESCAPE '!'"))
            params.append("%" + query.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%")
        where = SQL(" WHERE ") + SQL(" AND ").join(clauses) if clauses else SQL("")
        statement = SQL("SELECT id, memory, update_time, importance, event_date, keywords FROM {}{} "
                        "ORDER BY id DESC LIMIT %s").format(table, where)
        try:
            async with self.pool.connection() as conn:
                rows = await (await conn.execute(statement, (*params, limit + 1))).fetchall()
        except UndefinedTable:
            # A registered character can have no memory table yet. Do not create it on GET.
            rows = []
        return MemoryPage(items=[_record(row) for row in rows[:limit]],
                          next_cursor=_encode_cursor(character_id, query, rows[limit - 1]["id"])
                          if len(rows) > limit else None)

    async def get(self, character_id: str, memory_id: int) -> MemoryRecord:
        table = self._table(character_id)
        try:
            async with self.pool.connection() as conn:
                row = await (await conn.execute(SQL(
                    "SELECT id, memory, update_time, importance, event_date, keywords FROM {} WHERE id = %s"
                ).format(table), (memory_id,))).fetchone()
        except UndefinedTable:
            row = None
        if row is None:
            raise ServiceError("memory_not_found", "该角色下不存在这条记忆。", 404)
        return _record(row)
