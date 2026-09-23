# agent/memory/store.py
# 角色记忆存储：父-子表架构 + 语义分块 + 混合检索（向量 + 关键词 + 时间过滤）
#
# 架构:
#   父表 {character_name}:        id, memory, update_time, importance, event_date, keywords
#   子表 {character_name}_chunks: id, parent_id, chunk_text, chunk_index, embedding vector(1024)
#
# 写入:
#   - insert_memory / update_memory 自动切块编码写入子表
#   - 切块以 LLM 语义分块为主，LLM 不可用时回退到正则函数切割
#   - keywords / event_date 未显式传入时自动提取（LLM 优先，正则兜底）
#
# 检索 search_hybrid:
#   - 向量 + 关键词两路召回（子表），时间过滤下推（event_date 为 NULL 的记忆
#     在时间检索中排除）
#   - 两路分数各自除以本路最高分归一化后加权粗排
#     （importance / 时间衰减只加一次，衰减基准 update_time）
#   - Reranker 精排，按精排顺序严格重建结果

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from agent.classes.memory_job import MemoryOperation, PreparedMemory

from psycopg.sql import SQL, Identifier
from pgvector.psycopg import register_vector_async

from agent.utils.chunking import chunk_memory
from utils.daily_logger import get_logger, log_event
import re

logger = get_logger("memory.store")

# ---------------------------------------------------------------------------
# 混合检索权重默认值
# ---------------------------------------------------------------------------

_DEFAULT_VECTOR_WEIGHT = 0.55
_DEFAULT_KEYWORD_WEIGHT = 0.25
_DEFAULT_IMPORTANCE_WEIGHT = 0.10
_DEFAULT_TIME_DECAY_WEIGHT = 0.10
_DEFAULT_HALF_LIFE_SECONDS = 86400 * 30 * 3   # 90 days
_DEFAULT_SIMILARITY_THRESHOLD = 0.55
_DEFAULT_VECTOR_LIMIT = 30
_DEFAULT_KEYWORD_LIMIT = 30
_DEFAULT_FINAL_LIMIT = 15


# ---------------------------------------------------------------------------
# AsyncPostgresCharacterMemoryStore
# ---------------------------------------------------------------------------

class AsyncPostgresCharacterMemoryStore:
    """角色记忆存储（父-子表架构）。

    父表 ``{character_name}``:
        id, memory, update_time, importance, event_date, keywords

    子表 ``{character_name}_chunks``:
        id, parent_id, chunk_text, chunk_index, embedding vector(1024)
    """

    def __init__(self, pool, character_name: str):
        self.pool = pool
        self.character_name = character_name
        self.chunk_table = f"{character_name}_chunks"
        self._reranker_cache: object = None # Reranker 延迟缓存（类级别）

    # --------------- 工厂 & 模式初始化 ---------------

    @classmethod
    async def create(cls, pool, character_name: str):
        instance = cls(pool, character_name)
        await instance._init_table()
        return instance

    async def _init_table(self):
        """确保父表列完备 + 子表存在。"""
        async with self.pool.connection() as conn:
            await register_vector_async(conn)
            async with conn.cursor() as cur:
                # 父表
                await cur.execute(SQL("""
                    CREATE TABLE IF NOT EXISTS {} (
                        id              SERIAL PRIMARY KEY,
                        memory          TEXT NOT NULL,
                        update_time     TIMESTAMP NOT NULL,
                        importance      INTEGER NOT NULL,
                        event_date      DATE,
                        keywords        TEXT
                    )
                """).format(Identifier(self.character_name)))
                # 子表
                await cur.execute(SQL("""
                    CREATE TABLE IF NOT EXISTS {} (
                        id          SERIAL PRIMARY KEY,
                        parent_id   INTEGER NOT NULL,
                        chunk_text  TEXT    NOT NULL,
                        chunk_index INTEGER NOT NULL DEFAULT 0,
                        embedding   vector(1024)
                    )
                """).format(Identifier(self.chunk_table)))

                # 外键（幂等）
                fk_name = f"{self.chunk_table}_parent_id_fkey"
                await cur.execute(
                    "SELECT 1 FROM pg_constraint WHERE conname = %s", (fk_name,))
                if not (await cur.fetchone()):
                    await cur.execute(
                        SQL("ALTER TABLE {} ADD CONSTRAINT {} "
                            "FOREIGN KEY (parent_id) REFERENCES {}(id) "
                            "ON DELETE CASCADE").format(
                            Identifier(self.chunk_table),
                            Identifier(fk_name),
                            Identifier(self.character_name)))

                # B-tree 索引
                await cur.execute(SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {}(parent_id)"
                ).format(Identifier(f"idx_{self.character_name}_chunks_parent"),
                          Identifier(self.chunk_table)))
                await cur.execute(SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {}(chunk_index)"
                ).format(Identifier(f"idx_{self.character_name}_chunks_index"),
                          Identifier(self.chunk_table)))
                # 父表 event_date 索引（配合时间过滤下推）
                await cur.execute(SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {}(event_date)"
                ).format(Identifier(f"idx_{self.character_name}_event_date"),
                          Identifier(self.character_name)))

    # --------------- CRUD ---------------

    @asynccontextmanager
    async def write_session(self, conn):
        """角色库跨线程共享；所有常规写入口与后台整理使用相同角色锁。"""
        key = f"memory-character:{self.character_name}"
        try:
            await conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
            yield
        finally:
            if not conn.closed:
                await conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))

    async def prepare_memory(self, memory: str, importance: int | None = 0,
                             event_date: str = None, keywords: str = None) -> PreparedMemory:
        """完成所有模型计算；本方法不写库、不持有写事务。"""
        chunks, auto_keywords, auto_event_date = await chunk_memory(memory)
        from agent.utils.models import get_qwen_embedding_model

        def encode_chunks():
            encoder = get_qwen_embedding_model()
            return [(text, encoder.encode(text)) for chunk in chunks if (text := chunk.strip())]

        prepared = await asyncio.to_thread(encode_chunks)
        if not prepared:
            raise ValueError("记忆切块为空，不能提交缺少向量块的记录")
        return PreparedMemory(
            memory, importance,
            auto_event_date if event_date is None else event_date,
            auto_keywords if keywords is None else keywords,
            prepared,
        )

    async def apply_operations(self, conn, operations: list[MemoryOperation], *, strict=True,
                               changes: list | None = None) -> list:
        """仅执行 SQL 并收集审计内容；调用方必须在事务提交后输出 changes。"""
        results = []
        async with conn.cursor() as cur:
            for operation in operations:
                data = operation.prepared
                before = None
                if operation.name == "delete_memory":
                    await cur.execute(SQL("DELETE FROM {} WHERE id = %s RETURNING "
                                          "id, memory, update_time, importance, event_date, keywords").format(
                        Identifier(self.character_name)), (operation.memory_id,))
                    row = await cur.fetchone()
                    if strict and row is None:
                        raise ValueError("待删除记忆已不存在")
                    if row is not None and changes is not None:
                        changes.append(dict(operation=operation.name, before=dict(row), after=None))
                    results.append(row is not None)
                    continue
                if data is None:
                    raise ValueError("记忆写操作缺少计算产物")
                if operation.name == "insert_memory":
                    await cur.execute(SQL("""
                        INSERT INTO {} (memory, update_time, importance, event_date, keywords)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id, memory, update_time, importance, event_date, keywords
                    """).format(Identifier(self.character_name)),
                        (data.text, datetime.now(), data.importance or 0, data.event_date, data.keywords))
                elif operation.name == "update_memory":
                    await cur.execute(SQL("SELECT id, memory, update_time, importance, event_date, keywords "
                                          "FROM {} WHERE id = %s FOR UPDATE").format(
                        Identifier(self.character_name)), (operation.memory_id,))
                    before = await cur.fetchone()
                    await cur.execute(SQL("""
                        UPDATE {} SET memory = %s, update_time = %s,
                            importance = COALESCE(%s, importance), event_date = %s, keywords = %s
                        WHERE id = %s RETURNING id, memory, update_time, importance, event_date, keywords
                    """).format(Identifier(self.character_name)),
                        (data.text, datetime.now(), data.importance, data.event_date, data.keywords,
                         operation.memory_id))
                else:
                    raise ValueError(f"未知记忆写操作: {operation.name}")
                row = await cur.fetchone()
                if row is None:
                    if strict:
                        raise ValueError("待更新记忆已不存在")
                    results.append(None)
                    continue
                if operation.name == "update_memory":
                    await cur.execute(SQL("DELETE FROM {} WHERE parent_id = %s").format(
                        Identifier(self.chunk_table)), (row["id"],))
                for index, (text, embedding) in enumerate(data.chunks):
                    await cur.execute(SQL("""
                        INSERT INTO {} (parent_id, chunk_text, chunk_index, embedding)
                        VALUES (%s, %s, %s, %s)
                    """).format(Identifier(self.chunk_table)), (row["id"], text, index, embedding))
                results.append(row)
                if changes is not None:
                    changes.append(dict(operation=operation.name, before=dict(before) if before else None,
                                        after=dict(row)))
        return results

    def log_changes(self, changes, **context):
        """只记录已提交的父记忆完整内容，不包含向量块。"""
        for change in changes:
            row = change["after"] or change["before"]
            log_event(logger, "记忆变更已提交", character=self.character_name,
                      memory_id=row["id"], **context, **change)

    async def _commit_operation(self, operation: MemoryOperation):
        changes = []
        async with self.pool.connection() as conn:
            await register_vector_async(conn)
            async with self.write_session(conn):
                async with conn.transaction():
                    result = (await self.apply_operations(conn, [operation], strict=False, changes=changes))[0]
        self.log_changes(changes, source="direct")
        return result

    async def insert_memory(self, memory: str, importance: int = 0,
                            event_date: str = None, keywords: str = None):
        prepared = await self.prepare_memory(memory, importance, event_date, keywords)
        return await self._commit_operation(MemoryOperation("insert_memory", prepared=prepared))

    async def get_memories(self, limit: int = None):
        """返回父表记忆列表。"""
        select_sql = SQL("""
            SELECT id, memory, importance, event_date, keywords, update_time
            FROM {} ORDER BY id ASC
        """).format(Identifier(self.character_name))

        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                if limit is not None and limit > 0:
                    await cur.execute(select_sql + SQL(" LIMIT %s"), (limit,))
                else:
                    await cur.execute(select_sql)
                return await cur.fetchall()

    async def update_memory(self, memory_id: int, new_memory: str,
                            importance: int = None, event_date: str = None,
                            keywords: str = None):
        prepared = await self.prepare_memory(new_memory, importance, event_date, keywords)
        return await self._commit_operation(MemoryOperation("update_memory", memory_id, prepared))

    async def delete_memory(self, memory_id: int):
        return await self._commit_operation(MemoryOperation("delete_memory", memory_id))

    async def update_memory_top_field(self, memory_id: int, update_items: dict):
        """按 key 更新父表字段（用于批量操作如重编码）。"""
        if not update_items:
            raise ValueError("update_items 不能为空")

        set_clauses = [SQL("{} = %s").format(Identifier(k))
                       for k in update_items]
        update_sql = SQL("UPDATE {} SET {} WHERE id = %s RETURNING "
                         "id, memory, update_time, importance, event_date, keywords").format(
            Identifier(self.character_name),
            SQL(", ").join(set_clauses))
        values = list(update_items.values()) + [memory_id]

        async with self.pool.connection() as conn:
            await register_vector_async(conn)
            async with self.write_session(conn):
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(SQL("SELECT id, memory, update_time, importance, event_date, keywords "
                                              "FROM {} WHERE id = %s FOR UPDATE").format(
                            Identifier(self.character_name)), (memory_id,))
                        before = await cur.fetchone()
                        await cur.execute(update_sql, values)
                        updated = await cur.fetchone()
        if updated:
            self.log_changes([dict(operation="update_memory_top_field", before=dict(before),
                                   after=dict(updated))], source="direct")
        return updated is not None

    # --------------- 检索 ---------------

    def _get_reranker(self, top_k: int = 50):
        """获取 Reranker（models lru 缓存加载，实例级薄缓存）。"""
        if self._reranker_cache is None or self._reranker_cache.top_k != top_k:
            from agent.utils.models import get_reranker_model
            self._reranker_cache = get_reranker_model(top_k=top_k)
        return self._reranker_cache

    async def search_hybrid(self, query_embedding: list,
                            query_text: str = None,
                            keyword_text: str = None,
                            date_from: str = None, date_to: str = None,
                            use_reranker: bool = True,
                            rerank_top_k: int = 50,
                            half_life_seconds: int = _DEFAULT_HALF_LIFE_SECONDS,
                            similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
                            vector_limit: int = _DEFAULT_VECTOR_LIMIT,
                            keyword_limit: int = _DEFAULT_KEYWORD_LIMIT,
                            final_limit: int = _DEFAULT_FINAL_LIMIT,
                            vector_weight: float = _DEFAULT_VECTOR_WEIGHT,
                            keyword_weight: float = _DEFAULT_KEYWORD_WEIGHT,
                            importance_weight: float = _DEFAULT_IMPORTANCE_WEIGHT,
                            time_weight: float = _DEFAULT_TIME_DECAY_WEIGHT):
        """混合检索：向量 + 关键词 → 归一化合并 → 粗排 → Reranker 精排。

        流程:
          1. 向量召回（子表，时间过滤下推）→ 按 parent_id 聚合取 max
          2. 关键词召回（子表 JOIN 父表，时间过滤下推）→ 按 parent_id 聚合取 max
          3. 按 parent_id 合并，两路分数各自除以本路最高分归一化
          4. Python 计算 coarse_score（importance / 时间衰减只加一次）
          5. Reranker 对粗排 top-K 精排，按精排顺序返回 final_limit 条

        参数:
            query_embedding:  查询向量（list of float）
            query_text:       原始查询文本（供 reranker 使用）
            keyword_text:     关键词字符串（空格/逗号分隔）
            date_from:        yyyy-mm-dd 起始日期（None = 不限）
            date_to:          yyyy-mm-dd 结束日期（None = 不限）
            use_reranker:     是否启用 reranker 精排
            rerank_top_k:     送入 reranker 的粗排候选数量（建议 30~60）

        说明:
            - 时间过滤为严格匹配：event_date 为 NULL 的记忆在时间检索中排除。
            - 时间衰减以 update_time 为基准（更新记忆视为印象加深）。
        """
        kw_split: list[str] = []
        if keyword_text:
            kw_split = [w.strip() for w in re.split(r"[,，\s]+", keyword_text)
                        if w.strip()]

        lambda_decay = 1.0 / half_life_seconds

        # 时间过滤参数（两路共用，顺序: date_from, date_to）
        date_params: list = []
        if date_from is not None:
            date_params.append(date_from)
        if date_to is not None:
            date_params.append(date_to)

        # ---- 1 & 2: 两路召回（子表，各带时间过滤下推），同一连接 ----
        async with self.pool.connection() as conn:
            await register_vector_async(conn)
            async with conn.cursor() as cur:

                # 1. 向量召回 + 按 parent_id 聚合（取 max vector_score）
                v_date_clause = SQL("")
                if date_params:
                    conds = []
                    if date_from is not None:
                        conds.append(SQL("event_date >= %s"))
                    if date_to is not None:
                        conds.append(SQL("event_date <= %s"))
                    v_date_clause = SQL(
                        " AND c.parent_id IN (SELECT id FROM {} WHERE {})"
                    ).format(Identifier(self.character_name),
                             SQL(" AND ").join(conds))

                await cur.execute(
                    SQL("""
                        WITH v AS (
                            SELECT c.parent_id,
                                   MAX(1 - (c.embedding <=> %s)) AS vector_score
                            FROM {} c
                            WHERE (1 - (c.embedding <=> %s)) >= %s
                            {}
                            GROUP BY c.parent_id
                            ORDER BY vector_score DESC
                            LIMIT %s
                        )
                        SELECT p.id, p.memory, p.importance,
                               p.event_date, p.update_time, p.keywords,
                               v.vector_score
                        FROM v
                        JOIN {} p ON p.id = v.parent_id
                    """).format(
                        Identifier(self.chunk_table),
                        v_date_clause,
                        Identifier(self.character_name)),
                    (query_embedding, query_embedding, similarity_threshold,
                     *date_params, vector_limit))
                vector_rows = await cur.fetchall()

                # 2. 关键词召回 + 按 parent_id 聚合（取 max keyword_score）
                keyword_rows = []
                if kw_split:
                    kws = kw_split[:5]
                    like_patterns = [f"%{k}%" for k in kws]
                    or_trgm = SQL(" OR ").join(
                        [SQL("c.chunk_text %% %s")] * len(kws))
                    or_ilike = SQL(" OR ").join(
                        [SQL("p.keywords ILIKE %s")] * len(kws))
                    great_clause = SQL("GREATEST({})").format(
                        SQL(", ").join(
                            [SQL("similarity(c.chunk_text, %s)")] * len(kws)))

                    k_date_clause = SQL("")
                    if date_from is not None:
                        k_date_clause = k_date_clause + SQL(
                            " AND p.event_date >= %s")
                    if date_to is not None:
                        k_date_clause = k_date_clause + SQL(
                            " AND p.event_date <= %s")

                    await cur.execute(
                        SQL("""
                            SELECT p.id, p.memory, p.importance,
                                   p.event_date, p.update_time, p.keywords,
                                   MAX({}) AS keyword_score
                            FROM {} c
                            JOIN {} p ON p.id = c.parent_id
                            WHERE (({}) OR ({})) {}
                            GROUP BY p.id
                            ORDER BY keyword_score DESC
                            LIMIT %s
                        """).format(
                            great_clause,
                            Identifier(self.chunk_table),
                            Identifier(self.character_name),
                            or_trgm, or_ilike, k_date_clause),
                        (*kws,            # similarity args
                         *kws,            # %% args
                         *like_patterns,  # ILIKE args
                         *date_params,    # 时间过滤
                         keyword_limit))
                    keyword_rows = await cur.fetchall()

        # ---- 3. 合并：按 id 合并两路得分 + 归一化 ----
        merged: dict[int, dict] = {}
        for r in vector_rows:
            e = merged.setdefault(r["id"],
                                  {"row": dict(r), "vector": 0.0,
                                   "keyword": 0.0})
            e["row"] = dict(r)
            e["vector"] = max(e["vector"], r["vector_score"])
        for r in keyword_rows:
            e = merged.setdefault(r["id"],
                                  {"row": dict(r), "vector": 0.0,
                                   "keyword": 0.0})
            e["row"] = dict(r)
            e["keyword"] = max(e["keyword"], r["keyword_score"])

        if not merged:
            return []

        v_max = max(e["vector"] for e in merged.values())
        k_max = max(e["keyword"] for e in merged.values())

        # ---- 4. 粗排（Python 计算，importance / 时间衰减只加一次）----
        now = datetime.now()
        for e in merged.values():
            v_norm = e["vector"] / v_max if v_max > 0 else 0.0
            k_norm = e["keyword"] / k_max if k_max > 0 else 0.0
            row = e["row"]
            imp = (row["importance"] or 0) / 100.0
            update_time = row["update_time"]
            if update_time.tzinfo is not None:
                age = (datetime.now(timezone.utc)
                       - update_time).total_seconds()
            else:
                age = (now - update_time).total_seconds()
            decay = 1.0 / (1.0 + lambda_decay * max(age, 0.0))
            row["coarse_score"] = (
                v_norm * vector_weight
                + k_norm * keyword_weight
                + imp * importance_weight
                + decay * time_weight
            )

        coarse_rows = sorted((e["row"] for e in merged.values()),
                             key=lambda r: r["coarse_score"], reverse=True)

        # ---- 5. Reranker 精排 ----
        if use_reranker and query_text and len(coarse_rows) > 1:
            return await self._rerank(coarse_rows, query_text,
                                      rerank_top_k=rerank_top_k,
                                      final_limit=final_limit)
        return coarse_rows[:final_limit]

    async def _rerank(self, rows: list[dict], query_text: str,
                      rerank_top_k: int, final_limit: int) -> list[dict]:
        """对粗排候选用 Cross-Encoder Reranker 精排。

        通过 Document.metadata 携带候选下标，严格按 reranker 返回顺序
        重建结果（同时避免相同文本记忆被集合去重误杀）。
        rows 需包含 'memory' 键（父表记忆文本）。
        """
        from langchain_core.documents import Document

        # 截断送入 reranker 的候选数
        candidates = rows[:rerank_top_k]

        # 构造 (query, passage) pairs，metadata 携带候选下标
        docs = [Document(page_content=r["memory"], metadata={"idx": i})
                for i, r in enumerate(candidates)]

        reranker = self._get_reranker(top_k=final_limit)
        try:
            reranked = await reranker.acompress_documents(docs, query_text)
        except Exception as e:
            logger.warning(f"精排失败，回退粗排结果: {e}")
            return candidates[:final_limit]

        # 严格按 reranker 返回顺序重建结果
        result = [candidates[d.metadata["idx"]] for d in reranked]
        # 极端情况 reranker 返回数量不足时，按粗排顺序补齐
        if len(result) < final_limit:
            picked = {d.metadata["idx"] for d in reranked}
            for i, r in enumerate(candidates):
                if i not in picked:
                    result.append(r)
                    if len(result) >= final_limit:
                        break
        return result[:final_limit]
