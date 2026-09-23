import sys

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from config.config import DB_URL

if sys.platform.startswith("win"):
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pool: AsyncConnectionPool = None
checkpointer: AsyncPostgresSaver = None


async def init_db(*, checkpoints: bool = True):
    global pool, checkpointer

    if pool is not None:
        return
    if not DB_URL:
        raise RuntimeError("DB_URL is not configured. Set it in config/.env.")

    pool = AsyncConnectionPool(
        DB_URL,
        min_size=2,
        max_size=10,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row}
    )
    await pool.open()

    if checkpoints:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()


async def close_db():
    global pool, checkpointer

    if pool is not None:
        await pool.close()
    pool = None
    checkpointer = None
