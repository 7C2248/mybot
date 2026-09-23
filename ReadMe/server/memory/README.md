# 记忆服务：MemoryRuntime 与只读记忆浏览

## 职责与入口

本页覆盖 `server/services/memory_runtime.py`（后台记忆消费线程）与 `server/repositories/memories.py`（角色记忆原文只读浏览），并汇总 `GET /api/threads/{id}/memory-status` 的状态语义。记忆的队列结构、权限核对、处理与提交逻辑属于 `agent/memory/*`，本页只说明服务侧如何启动、加锁、交接与查询，不重复其实现。

| 文件 | 类型 | 职责 |
| --- | --- | --- |
| [server/services/memory_runtime.py](../../../server/services/memory_runtime.py) | 执行器 | `MemoryRuntime`：独立线程/事件循环/连接池消费持久记忆队列 |
| [server/repositories/memories.py](../../../server/repositories/memories.py) | 仓储 | `MemoryRepository`：现有父表原文搜索与分页（不加载向量模型、不建表） |
| [server/routes/memories.py](../../../server/routes/memories.py) | 路由 | `/api/characters/{id}/memories` 列表与详情 |
| [server/routes/threads.py](../../../server/routes/threads.py) | 路由 | `memory-status` 查询（仓储实现见 [conversations 叶子](../conversations/README.md)） |

上游：`RunRuntime.__init__` 创建 `MemoryRuntime`，`RunRuntime.start` 调用其 `start()`；`app.py` lifespan 关闭时经 `RunRuntime.close` 调用其 `close()`。下游：`agent/memory/jobs.py` 的 `MemoryJobRepository`、`agent/memory/worker.py` 的 `MemoryWorker`。

## 调用链总览

```text
构建阶段
B1 MemoryRuntime.__init__ ── 保存 settings/models/worker_factory，初始化状态
B2 MemoryRuntime.start ── 条件满足时启动 "mybot-memory" 线程并等待就绪

运行阶段（记忆线程）
R1 MemoryRuntime._thread_main ── 新事件循环内运行 _serve
R2 MemoryRuntime._serve ── 打开独立连接池 → MemoryJobRepository.create → MemoryWorker → 轮询
  R2.1 models.memory_gate.acquire(blocking=False) ── 与模型配置应用互斥
  R2.2 models.snapshot() + model_config_scope(data) ── 使用生效配置快照
  R2.3 worker.run_once() ── 队列领取与处理（agent 侧，见链接文档）
R3 MemoryRuntime.close ── 取消任务并等待线程
R4 MemoryRuntime.active_job ── 供 memory-status 判定 running

只读浏览（HTTP 线程）
R5 MemoryRepository.list ── 字面子串搜索 + ID 倒序游标分页
R6 MemoryRepository.get ── 角色范围内详情
辅助：_keywords / _record / _query_hash / _encode_cursor / _decode_cursor / _table
```

## 构建链

### B1. `MemoryRuntime.__init__`

- 定位与签名：[server/services/memory_runtime.py](../../../server/services/memory_runtime.py) 的 `def __init__(self, settings, models, *, worker_factory=None)`（同步）。
- 调用方与条件：`RunRuntime.__init__`。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `settings` | `ServiceSettings` | 必填 | 读取 `db_url`、`enable_runs`、`memory_worker` |
| `models` | `ModelSettingsService` | 必填 | 提供 `memory_gate`、`snapshot()`、`active` |
| `worker_factory` | `Callable[[jobs], MemoryWorker] \| None` | 默认 `None` | 测试注入受控 Worker；为 `None` 时用 `agent.memory.worker.MemoryWorker` |

功能：初始化 `ready=False`、`error_code=None`、`started=Future()`、`stopping=threading.Event()`、`thread=loop=task=worker=None`。
输出：实例。`active_job` 属性返回 `getattr(self.worker, 'active_job', None)`（Worker 处理中为 `{id, thread_id, character_name}`，否则 `None`）。

### B2. `MemoryRuntime.start`

- 定位与签名：`async def start(self)`。
- 调用方与条件：`RunRuntime.start`。

功能：`not settings.db_url or not settings.enable_runs or not settings.memory_worker` → 直接返回（`ready` 保持 `False`，`memory-status` 仍可按数据库状态返回）。否则启动 `threading.Thread(target=self._thread_main, name='mybot-memory', daemon=True)`，`await asyncio.wrap_future(self.started)` 等待初始化。
输出：无返回值。异常：初始化失败使 `started` 抛 `RuntimeError('Memory runtime initialization failed')`；`RunRuntime.start` 捕获后关闭记忆并继续对话服务。

## 运行链

### R1. `MemoryRuntime._thread_main`

- 定位与签名：`def _thread_main(self)`（线程入口）。
- 功能：与对话执行器一致，从 `server.__main__` 导入 `create_event_loop`，在 `asyncio.Runner` 中运行 `self._serve()`。捕获 `BaseException`：`error_code='memory_runtime_unavailable'`、记录“后台记忆执行器异常停止”；`started` 未完成则 `set_exception`；`finally` 置 `ready=False`。

### R2. `MemoryRuntime._serve`

- 定位与签名：`async def _serve(self)`。
- 调用方与条件：记忆线程事件循环。

初始化：
1. 延迟导入 `config.model_config.model_config_scope`、`agent.memory.jobs.MemoryJobRepository`、`agent.memory.worker.MemoryWorker`。
2. `self.loop = asyncio.get_running_loop()`；`self.task = asyncio.current_task()`（供 `close` 取消）。
3. `database = Database(self.settings)`；`await database.open()`（记忆线程独立连接池）。
4. `jobs = await MemoryJobRepository.create(database.pool)`：内部 `setup()` 执行 `agent/memory/migrations/001_memory_jobs.sql`，创建 `memory_service.jobs/results`（幂等 DDL，见 [persistence 叶子](../persistence/README.md)）。
5. `self.worker = (self.worker_factory or MemoryWorker)(jobs)`；`self.ready = True`；`self.started.set_result(None)`。

轮询循环（`while not self.stopping.is_set()`）：
1. `if self.models.memory_gate.acquire(blocking=False):` —— 该门锁只保护“模型设置应用”，与对话门锁 `gate` 相互独立。
2. `try`：`if self.models.active is not None:` → `data, _ = self.models.snapshot()` → `with model_config_scope(data): worked = await self.worker.run_once()`（任务级配置快照与独立节点模型缓存）。成功执行后若 `error_code` 非空，记录“后台记忆队列已恢复”并清零。
3. `except Exception`：仅首次记录“后台记忆队列不可用”，置 `error_code='memory_runtime_unavailable'`；`finally` 释放 `memory_gate`。
4. 未执行任务（门锁未取得、无生效配置或队列为空）→ `await asyncio.sleep(0.5)`。
5. `except asyncio.CancelledError: pass`；`finally: ready=False; await database.close()`。

输出：无返回值。语义：记忆执行失败不回滚已提交的对话正文；失败/中断的任务保留在持久队列，重启后继续消费（队列细节见 [memory/jobs 叶子](../../agent/memory/jobs/README.md) 与 [memory/worker 叶子](../../agent/memory/worker/README.md)）。

### R3. `MemoryRuntime.close`

- 定位与签名：`async def close(self)`。
- 调用方：`RunRuntime.close`（先创建 `memory.close()` 任务，再等待对话线程结束）。

功能：`ready=False`；`stopping.set()`；线程存活时 `self.loop.call_soon_threadsafe(self.task.cancel)`，`await asyncio.to_thread(self.thread.join)`。原生推理不可安全强杀，等待线程自然结束；未完成任务留在持久队列。

### R4. `active_job` 与 `memory-status` 状态语义

- `MemoryRuntime.active_job`：读取 Worker 的 `active_job`（`agent/memory/worker.py` 在 `_process` 开始时设置、`finally` 清空）。
- `GET /api/threads/{thread_id}/memory-status` 由 `ThreadRepository.memory_status(thread_id, active_job)` 实现（完整判定逻辑见 [conversations 叶子](../conversations/README.md) R12.1）。状态表：

| status | 产生条件 | 含义 |
| --- | --- | --- |
| `disabled` | 会话 `memory_storage_enabled=false` | 不创建记忆任务、不调用记忆队列 |
| `idle` | 无 `memory_service.jobs` 行、无 `results` 行 | 无后台记忆活动 |
| `pending` | 有任务行且状态为 `pending`；或独立进程 Worker 正在执行（本服务看不到其内存态） | 排队等待 |
| `running` | 任务行的 `id/thread_id/character_name` 与本服务 `active_job` 一致 | 本服务的记忆线程正在处理 |
| `failed` | 任务行状态为 `failed`（达到最大尝试次数） | 需要人工/`--retry` 处理，队列保留 |
| `completed` | 无任务行，但 `memory_service.results` 有该角色/图线程的最近 `job_id` | 最近一项结果已提交 |

响应附 `job_id`（有任务或结果时）。服务级 `GET /api/service` 另报告 `memory_runtime_ready` / `memory_runtime_error`。迁移自原 `server/README.md` 的前端行为记录（未在本轮核对前端源码）：界面每 5 秒独立刷新活动会话的后台状态，读取失败只显示记忆状态暂不可用，仍可发送对话。

### R5. `MemoryRepository.list`

- 定位与签名：[server/repositories/memories.py](../../../server/repositories/memories.py) 的 `async def list(self, character_id, *, query="", cursor=None, limit=20) -> MemoryPage`。
- 调用方：`GET /api/characters/{character_id}/memories`（`query` ≤1000、`cursor` ≤2048、`limit` 1..100）。
- 构造：`MemoryRepository(database.pool, catalog, settings.memory_schema)`，每请求新建。

| 输入 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `character_id` | `str` | 路径 | 必须已注册（`catalog.get`） | 定位记忆父表 |
| `query` | `str` | 查询参数 | 默认 `""` | 字面子串过滤 |
| `cursor` | `str \| None` | 查询参数 | 默认 `None` | 读取更早（更小 id）记录 |
| `limit` | `int` | 查询参数 | 默认 20 | 每页条数 |

功能与内部调用：
1. `table = self._table(character_id)`：`Identifier(self.schema, character.id)`——schema 来自 `settings.memory_schema`，表名用注册 ID 而非 URL 段，动态 SQL 全部参数化/标识符化。
2. `before = _decode_cursor(cursor, character_id, query)`：游标为 `[1, character_id, sha256(query), before_id]` 的 base64；角色或查询哈希不符、id 非正整数 → 400 `invalid_cursor`。
3. 组装 `WHERE`：`id < before`（有游标时）与 `memory ILIKE %s ESCAPE '!'`（有查询时）；搜索前对 `query` 转义 `!`、`%`、`_`，因此通配符按普通字符处理；`ILIKE` 大小写不敏感。
4. `SELECT id, memory, update_time, importance, event_date, keywords ... ORDER BY id DESC LIMIT limit+1`。
5. `psycopg.errors.UndefinedTable` → 视为空列表（**GET 不建表**；注册角色可能尚无记忆表）。
6. `MemoryPage(items=[_record(row) for row in rows[:limit]], next_cursor=_encode_cursor(character_id, query, rows[limit-1]["id"]) if len(rows) > limit else None)`。

输出：`MemoryPage`（`items` + `next_cursor`）。`_record(row)`：`id=str(row["id"])`、`memory`、`update_time`、`importance`、`event_date`、`keywords=_keywords(row["keywords"])`（逗号/中文逗号/分号/换行分隔或旧 JSON 数组，去重、去空白，不改写数据库原值）。

### R6. `MemoryRepository.get`

- 定位与签名：`async def get(self, character_id, memory_id) -> MemoryRecord`。
- 调用方：`GET /api/characters/{character_id}/memories/{memory_id}`（`memory_id` 1..2^63-1）。
- 功能：同 `_table` → `SELECT ... WHERE id=%s`；`UndefinedTable` 按无行处理；无行 → 404 `memory_not_found`；否则 `_record(row)`。
- 边界：详情同时限定角色与记忆 ID；不加载 Embedding/Reranker，不产生写操作。

### R7. 路由层

| 方法 | 路径 | 处理函数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/characters/{character_id}/memories` | `memories()` | `_repository()` 先 `catalog.get`，`database.pool is None` → 503 `database_unavailable`；转 `MemoryRepository.list` |
| GET | `/api/characters/{character_id}/memories/{memory_id}` | `memory()` | 同上转 `MemoryRepository.get` |

## 分支与异常链

| 条件 | 处理 | 终点 |
| --- | --- | --- |
| 未配置数据库 | 路由 `_repository` 抛 503 `database_unavailable` | R7 |
| 未注册角色 | `catalog.get` 抛 404 `character_not_found` | R7 |
| 记忆父表不存在 | `UndefinedTable` → 空列表/404，**不建表** | R5/R6 |
| 游标角色或查询不符 | 400 `invalid_cursor` | R5 |
| 记忆 ID 不存在 | 404 `memory_not_found` | R6 |
| `MYBOT_API_RUNS=0` 或 `MYBOT_API_MEMORY_WORKER=0` | `MemoryRuntime.start` 不建线程；队列保留 | B2 |
| 记忆初始化失败 | 对话服务继续；`GET /service` 报告错误；重启可重新初始化 | B2/R1 |
| 记忆任务执行失败 | 队列保留、退避重试（最多 5 次后 `failed`）；不影响已提交对话 | R2（细节见 agent 文档） |
| 模型配置应用与记忆并发 | `memory_gate` 非阻塞互斥；应用时若有记忆任务 → 409 `runtime_busy` | R2.1（见 [settings 叶子](../settings/README.md)） |

## 输入输出示例

列表（R5，虚构角色 `小满`，`query="咖啡"`）：

```json
{"items": [{"id": "42", "memory": "上周和小满一起去了咖啡店。", "importance": 3,
            "event_date": "2026-09-12", "update_time": "2026-09-12T20:00:00+08:00",
            "keywords": ["咖啡", "约定"]}],
 "next_cursor": "eyJ..."}
```

`memory-status`（R4）：

```json
{"status": "running", "job_id": 18}
{"status": "disabled"}
{"status": "completed", "job_id": 17}
```

## 关联文档与验证依据

- 上级：[服务总览](../README.md)；系统总览：[README/README.md](../../README.md)
- 同级：[conversations](../conversations/README.md)（`memory_status` 实现、策略版本、`memory_messages`）· [runtime](../runtime/README.md)（创建与关闭、记忆节点调用）· [persistence](../persistence/README.md)（`memory_service` 表）
- Agent 侧（权威实现，本页不重复）：[agent/memory/README.md](../../agent/memory/README.md) · [memory/worker/README.md](../../agent/memory/worker/README.md) · [memory/jobs/README.md](../../agent/memory/jobs/README.md) · [memory/policy/README.md](../../agent/memory/policy/README.md) · [memory/processor/README.md](../../agent/memory/processor/README.md) · [memory/store/README.md](../../agent/memory/store/README.md) · [node/memory/README.md](../../agent/node/memory/README.md)
- 配置：[config/README.md](../../config/README.md)
- 测试依据：[tests/server/test_memory_runtime.py](../../../tests/server/test_memory_runtime.py)、[tests/server/test_postgres.py](../../../tests/server/test_postgres.py)、[tests/test_memory_service.py](../../../tests/test_memory_service.py)、[tests/test_memory_service_postgres.py](../../../tests/test_memory_service_postgres.py)
- 迁移自原 `server/README.md`（原文件已移除）的规则：记忆从现有父表读取原文，不生成摘要；按 ID 倒序分页，默认 20、最多 100 条；query 最长 1000 字符；游标限定角色和查询条件；缺表返回空列表且 GET 不建表；详情同时校验角色和记忆 ID；旧无时区时间值不擅自附加时区；默认启用记忆 Worker，可能处理业务库中已排队的记忆任务，仅查看 API 时可设 `MYBOT_API_RUNS=0`；不要同时启动多个服务执行器。
- 验证记录（迁移自原文档，未在本次编写中重跑）：2026-09-22 后台记忆分离验收通过，覆盖受控处理器阻塞记忆事件循环时下一轮对话仍完成、记忆失败和初始化失败不影响对话、停止后任务保留及重启消费、并发模型配置与缓存隔离。
