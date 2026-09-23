# scripts/rebuild_character_memory.py
"""
角色记忆子表重建脚本（对应新版 agent/memory/store.py）

两种模式:
  1. 全量重建（默认）: 对父表所有记忆重新切块 → 重编码 → 重建子表
     python scripts/rebuild_character_memory.py --character_name SuLi

  2. 按 ID 重建: 只重建指定 ID 列表中的记忆（修改 rebuild_by_ids 内的列表）
     python scripts/rebuild_character_memory.py --character_name SuLi --ids

行为:
  - 重新切块（LLM 语义分块优先，失败回退函数切割；--regex-only 可强制函数切割）
  - 重新编码并重建 chunks 子表
  - 刷新父表 keywords / event_date（自动提取）

注意：不修改父表 memory / importance / update_time，
      保留 update_time 作为时间衰减基准不被重建刷新。
      旧版父表 embadding 整段向量列已废弃，本脚本不再写入。
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # 将 project/ 加入路径

from psycopg.sql import SQL, Identifier
from pgvector.psycopg import register_vector_async

from agent.utils.models import get_qwen_embedding_model
from agent.memory.store import AsyncPostgresCharacterMemoryStore
from agent.utils.chunking import (
    chunk_memory,
    split_memory_text,
    extract_keywords,
    extract_event_date,
)
from core.db import close_db, init_db
import core.db
from utils.daily_logger import get_logger

logger = get_logger("rebuild")

BATCH_SLEEP = 0.5   # 每条记忆处理间隔（秒），避免 LLM API 限流


async def rebuild_chunks_for_memory(memory_store, memory_row: dict,
                                    encoder, regex_only: bool) -> int:
    """重建单条记忆的子块并刷新 keywords / event_date，返回子块数。"""
    memory_id = memory_row["id"]
    memory_text = memory_row["memory"] or ""
    if not memory_text.strip():
        logger.info(f"[SKIP] id={memory_id}: 空文本")
        return 0

    # 1. 切块 + 提取元数据
    if regex_only:
        chunks = split_memory_text(memory_text)
        keywords = extract_keywords(memory_text)
        event_date = extract_event_date(memory_text)
    else:
        chunks, keywords, event_date = await chunk_memory(memory_text)

    # 2. 删除旧子块 + 重编码写入
    async with memory_store.pool.connection() as conn:
        await register_vector_async(conn)
        async with conn.cursor() as cur:
            await cur.execute(
                SQL("DELETE FROM {} WHERE parent_id = %s").format(
                    Identifier(memory_store.chunk_table)),
                (memory_id,))

            count = 0
            for idx, chunk_text in enumerate(chunks):
                chunk_text = chunk_text.strip()
                if not chunk_text:
                    continue
                # 直接传 numpy 数组（pgvector 适配器未注册 list dumper）
                embedding = encoder.encode(chunk_text)
                await cur.execute(
                    SQL("""
                        INSERT INTO {} (parent_id, chunk_text, chunk_index, embedding)
                        VALUES (%s, %s, %s, %s)
                    """).format(Identifier(memory_store.chunk_table)),
                    (memory_id, chunk_text, idx, embedding))
                count += 1

    # 3. 刷新父表 keywords / event_date（不触碰 update_time）
    await memory_store.update_memory_top_field(memory_id, {
        "keywords": keywords,
        "event_date": event_date,
    })
    return count


async def rebuild_memory(character_name: str, regex_only: bool = False):
    await init_db()
    try:
        memory_store = await AsyncPostgresCharacterMemoryStore.create(
            core.db.pool,
            character_name,
        )
        memories = await memory_store.get_memories()
        encoder = get_qwen_embedding_model()

        logger.info(f"角色={character_name} 共 {len(memories)} 条记忆, "
                    f"regex_only={regex_only}")

        total = len(memories)
        total_chunks = 0
        for i, memory in enumerate(memories, 1):
            try:
                count = await rebuild_chunks_for_memory(
                    memory_store, memory, encoder, regex_only)
                total_chunks += count
                logger.info(f"[{i}/{total}] [OK] id={memory['id']} chunks={count}")
            except Exception as e:
                logger.error(f"[{i}/{total}] [FAIL] id={memory['id']} "
                             f"err={type(e).__name__}: {e}")

            if not regex_only:
                await asyncio.sleep(BATCH_SLEEP)

        logger.info(f"完成: {total} 条记忆, {total_chunks} 个子块")
        return total, total_chunks
    finally:
        await close_db()


async def rebuild_by_ids(character_name: str, regex_only: bool = False):
    """按指定 ID 列表重建特定记忆的子块。

    修改本函数内 TO_REBUILD 列表后运行：
      python scripts/rebuild_character_memory.py --character_name SuLi --ids
    """
    # ================================================================
    # 手动编辑此列表（填入需要重建的记忆 ID）
    # ================================================================
    TO_REBUILD: list[int] = [
        # 1, 2, 3,
        320,322,326
    ]
    # ================================================================

    if not TO_REBUILD:
        logger.info("TO_REBUILD 列表为空，请在脚本中填入需要重建的记忆 ID "
                    "后重新运行")
        return

    await init_db()
    try:
        memory_store = await AsyncPostgresCharacterMemoryStore.create(
            core.db.pool, character_name)
        encoder = get_qwen_embedding_model()

        # 按 ID 列表查询目标记忆
        async with memory_store.pool.connection() as conn:
            await register_vector_async(conn)
            async with conn.cursor() as cur:
                await cur.execute(
                    SQL("SELECT * FROM {} WHERE id = ANY(%s)").format(
                        Identifier(character_name)),
                    (TO_REBUILD,))
                target_memories = await cur.fetchall()

        if not target_memories:
            logger.info(f"未在 {character_name} 中找到以下 ID: "
                        f"{TO_REBUILD}")
            return

        found_ids = {m["id"] for m in target_memories}
        missing = [i for i in TO_REBUILD if i not in found_ids]
        if missing:
            logger.info(f"以下 ID 不存在，跳过: {missing}")

        total = len(target_memories)
        total_chunks = 0
        logger.info(f"角色={character_name} 指定 {len(TO_REBUILD)} 个 ID, "
                    f"命中 {total} 条, regex_only={regex_only}")

        for i, memory in enumerate(target_memories, 1):
            try:
                count = await rebuild_chunks_for_memory(
                    memory_store, memory, encoder, regex_only)
                total_chunks += count
                logger.info(f"[{i}/{total}] [OK] id={memory['id']} chunks={count}")
            except Exception as e:
                logger.error(f"[{i}/{total}] [FAIL] id={memory['id']} "
                             f"err={type(e).__name__}: {e}")

            if not regex_only:
                await asyncio.sleep(BATCH_SLEEP)

        logger.info(f"完成: {total} 条记忆, {total_chunks} 个子块")
        return total, total_chunks
    finally:
        await close_db()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="重建角色记忆 chunks 子表（LLM 分块 + 重编码 + 元数据刷新）")
    parser.add_argument("--character_name", default="SuLi",
                        help="角色记忆表名（默认 SuLi）")
    parser.add_argument("--ids", default=True,
                        help="按函数内 TO_REBUILD 列表重建指定 ID（而非全量）")
    parser.add_argument("--regex-only", action="store_true",
                        help="强制使用函数切割（不调用 LLM，快速重建）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.ids:
        asyncio.run(rebuild_by_ids(args.character_name, args.regex_only))
    else:
        asyncio.run(rebuild_memory(args.character_name, args.regex_only))
    return 0


if __name__ == "__main__":
    sys.exit(main())
