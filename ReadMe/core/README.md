# core（数据库与 checkpoint 生命周期）

`core/` 提供 CLI/独立脚本共用的 Postgres 连接池与 LangGraph checkpoint 生命周期。本机 API 服务使用自己的连接池与执行器连接（见 [../server/README.md](../server/README.md)），不依赖本模块。

## 模块

| 名称 | 类型 | 主要功能 | 源码 |
| --- | --- | --- | --- |
| `core/db.py` | 基础设施 | 初始化/关闭全局连接池与 `AsyncPostgresSaver` | [db.py](../../core/db.py) |
| `core/__init__.py` | 包标记 | 模块说明 | [__init__.py](../../core/__init__.py) |

## 调用链

### `init_db`

- 定位与签名：`async def init_db(*, checkpoints: bool = True)`，[core/db.py:18](../../core/db.py#L18)。
- 调用方：`agent/memory/worker.py`（`checkpoints=False`）、维护脚本与测试；`server/` 不调用。
- 行为：
  1. 已初始化（`pool is not None`）时直接返回。
  2. `DB_URL` 为空时抛 `RuntimeError("DB_URL is not configured...")`。
  3. 创建 `AsyncConnectionPool(DB_URL, min_size=2, max_size=10, open=False, kwargs={"autocommit": True, "row_factory": dict_row})` 并 `open()`。
  4. `checkpoints=True` 时创建 `AsyncPostgresSaver(pool)` 并 `await checkpointer.setup()` 建立 checkpoint 表。
- 输出：模块级 `pool`、`checkpointer`。
- 隐式输入：`config.config.DB_URL`；模块顶部在 Windows 下设置 `WindowsSelectorEventLoopPolicy`（必须在创建事件循环前导入）。
- 异常：数据库不可用、缺少 `DB_URL` 时向上抛出；调用方决定退出。

### `close_db`

- 定位与签名：`async def close_db()`，[core/db.py:40](../../core/db.py#L40)。
- 行为：关闭连接池并把 `pool`/`checkpointer` 重置为 `None`；可重复调用。

## 边界与依赖

- **与 Agent 的关系**：`checkpointer` 在 `build_rp_agent(..., checkpointer=...)` 时传入；CLI 旧路径与独立 Worker 使用本模块。
- **与记忆的关系**：Worker 只需连接池（`checkpoints=False`），不创建 checkpoint 表。
- **安全**：服务端创建 checkpointer 时传入 `JsonPlusSerializer(allowed_msgpack_modules=None)` 限制反序列化类型（见 [server/services/runtime.py](../../server/services/runtime.py) 与 [LangGraph_CheckPoint_Postgres.md](../../data/docs/LangGraph_CheckPoint_Postgres.md)，后者存放于本地 `data/docs/`）。

## 阅读导航

- 上级：[系统总览](../README.md) · 配置：[config/README.md](../config/README.md)
- 使用方：[agent/memory/worker/README.md](../agent/memory/worker/README.md) · [server/README.md](../server/README.md)
