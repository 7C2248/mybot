# 数据库与迁移：Database 与 mybot_ui / memory_service 表

## 职责与入口

本页覆盖 `server/repositories/database.py` 与 `server/migrations/*.sql`：应用自有的异步连接池、`search_path`、幂等校验和迁移，以及 `mybot_ui` schema 001~004 的表目录；同时记录由 Agent 侧创建的 `memory_service` schema。

| 文件 | 类型 | 职责 |
| --- | --- | --- |
| [server/repositories/database.py](../../../server/repositories/database.py) | 基础设施 | `Database.open/ping/close`、`apply_migrations` |
| [server/migrations/001_service_tables.sql](../../../server/migrations/001_service_tables.sql) | SQL | `threads/runs/messages/run_events` |
| [server/migrations/002_runs_and_speech.sql](../../../server/migrations/002_runs_and_speech.sql) | SQL | 恢复/版本字段与 `speech_jobs` |
| [server/migrations/003_conversation_management.sql](../../../server/migrations/003_conversation_management.sql) | SQL | 会话策略、回收站、`cli_threads`、`audio_cleanup` |
| [server/migrations/004_checkpoint_detached_messages.sql](../../../server/migrations/004_checkpoint_detached_messages.sql) | SQL | 检查点确认的用户消息解除运行关联 |
| [agent/memory/migrations/001_memory_jobs.sql](../../../agent/memory/migrations/001_memory_jobs.sql) | SQL | `memory_service.jobs/results`（由 Agent 侧创建） |

上游：`app.py` lifespan 的 `database_factory(settings)` 与 `database.open()`；执行线程/记忆线程各自 `Database(settings)`。下游：全部仓储（[conversations](../conversations/README.md)、[speech](../speech/README.md)、[memory](../memory/README.md)）与 `readiness`（[settings 叶子](../settings/README.md)）。

## 调用链总览

```text
构建阶段
B1 Database.__init__ ── 保存 settings，pool/schema_version 置空
B2 Database.open ── 建池（search_path、超时、语句/锁超时）→ 打开 → apply_migrations
  B2.1 apply_migrations ── 事务 + advisory lock → schema_migrations 校验和比对 → 逐版本执行

运行阶段
R1 Database.ping ── 校验当前迁移版本行存在
R2 Database.close ── 关闭连接池并清空状态
```

## 构建链

### B1. `Database.__init__`

- 定位与签名：[server/repositories/database.py](../../../server/repositories/database.py) 的 `def __init__(self, settings: ServiceSettings)`。
- 输入：`settings`（读取 `db_url`、`db_timeout`、`memory_schema`）。输出：`pool=None`、`schema_version=None` 的空实例。

### B2. `Database.open`

- 定位与签名：`async def open(self)`。
- 调用方与条件：lifespan 的 HTTP 池、`RunRuntime._serve` 与 `MemoryRuntime._serve` 的执行线程池（**三个独立连接池**）。

功能：
1. `self.pool is not None or not self.settings.db_url` → 直接返回（未配置数据库时所有查询接口由路由返回 503）。
2. `configure(conn)`：`SET search_path TO <memory_schema>, public`（`memory_schema` 经 `Identifier` 引用，默认 `public`）。含义：未加 schema 前缀的查询优先落到记忆 schema，再回退 `public`。
3. 创建 `AsyncConnectionPool(self.settings.db_url, min_size=1, max_size=5, open=False, timeout=self.settings.db_timeout, check=AsyncConnectionPool.check_connection, configure=configure, kwargs={...})`：

| kwargs | 值 | 含义 |
| --- | --- | --- |
| `autocommit` | `True` | 单条语句自动提交；显式事务由仓储 `conn.transaction()` 开启 |
| `row_factory` | `dict_row` | 查询返回字典行 |
| `connect_timeout` | `max(1, int(db_timeout))` | 连接建立超时（秒） |
| `options` | `-c statement_timeout=5000 -c lock_timeout=5000` | 单语句 5 秒、等锁 5 秒上限 |

4. `await self.pool.open(wait=True, timeout=self.settings.db_timeout)`：在超时内确保最小连接可用。
5. `self.schema_version = await apply_migrations(self.pool)`。
6. `except BaseException:` → `await self.close()` 后重新抛出（连接或迁移失败拒绝启动）。

输出：无返回值；副作用为建池与迁移。

### B2.1 `apply_migrations`

- 定位与签名：`async def apply_migrations(pool, directory: Path = MIGRATIONS) -> str`；`MIGRATIONS = server/migrations`。
- 调用方：`Database.open`；测试可传自定义目录。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `pool` | `AsyncConnectionPool` | 必填 | 迁移执行连接 |
| `directory` | `Path` | 默认 `server/migrations` | 迁移目录 |

功能：
1. `files = sorted(directory.glob("[0-9]*.sql"))`；为空 → `RuntimeError("Service migrations are missing")`。
2. `sources = {path.stem: path.read_text(encoding="utf-8")}`。`read_text` 采用通用换行翻译，**CRLF 与 LF 归一后再哈希**，因此 Git 在 Windows/Linux 检出后校验和一致。
3. 单事务内：
   - `pg_advisory_xact_lock(hashtextextended('mybot-ui-migrations', 0))`：并发启动的服务串行迁移。
   - `CREATE SCHEMA IF NOT EXISTS mybot_ui`。
   - `CREATE TABLE IF NOT EXISTS mybot_ui.schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())`。
   - 读取已应用 `{version: checksum}`；若存在本服务目录没有的版本 → `RuntimeError("Database service schema is newer than this server")`（不允许旧服务操作新库）。
   - 逐版本：`sha256(source)`；已应用且校验和不一致 → `RuntimeError("An applied service migration has changed")`（**不得修改已应用迁移，应新增版本**）；未应用则执行整份 SQL 并插入版本与校验和。
4. 返回 `files[-1].stem`（最新版本号，赋给 `Database.schema_version`）。

输出：最后迁移版本字符串。异常：上述三种 `RuntimeError` 均在事务内抛出并回滚；`Database.open` 关闭连接池后向上传播，服务拒绝启动。

## 运行链

### R1. `Database.ping`

- 定位与签名：`async def ping(self) -> bool`。
- 调用方：`readiness`（`GET /ready`、`GET /settings`）。
- 功能：`pool is None or schema_version is None` → `False`；否则 `SELECT version FROM mybot_ui.schema_migrations WHERE version=%s`，行存在返回 `True`。
- 输出：布尔值。语义：只验证当前版本记录可读，不检测模型凭据或运行能力。

### R2. `Database.close`

- 定位与签名：`async def close(self)`。
- 功能：`try: await pool.close()`；`finally: pool=None; schema_version=None`。幂等，可在 `open` 失败路径重复调用。

## `mybot_ui` 表目录（迁移 001~004）

### 001：基础表

| 表 | 列与约束 | 索引 |
| --- | --- | --- |
| `threads` | `id UUID PK`；`character_id TEXT NOT NULL`；`graph_thread_id TEXT NOT NULL UNIQUE`；`title TEXT NOT NULL`；`created_at/updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`；`state JSONB NOT NULL DEFAULT '{}'`；`CHECK (graph_thread_id = 'ui:' || id::text)`；`CHECK (jsonb_typeof(state)='object')` | `threads_updated (updated_at DESC, id DESC)` |
| `runs` | `id UUID PK`；`thread_id UUID NOT NULL REFERENCES threads(id)`；`user_message_id UUID NOT NULL`；`client_request_id TEXT NOT NULL CHECK length 1..200`；`payload_hash TEXT NOT NULL`；`status TEXT NOT NULL DEFAULT 'queued' CHECK IN (queued,running,completed,completed_with_warnings,failed,interrupted)`；`phase TEXT NOT NULL DEFAULT 'queued'`；`retry_of UUID`；`error_code TEXT`；`warnings JSONB NOT NULL DEFAULT '[]' CHECK jsonb_typeof='array'`；`created_at/started_at/finished_at`；`UNIQUE (id, thread_id)`；`UNIQUE (thread_id, client_request_id)`；`FOREIGN KEY (retry_of, thread_id) REFERENCES runs (id, thread_id)` | `one_active_run_per_thread UNIQUE (thread_id) WHERE status IN ('queued','running')`；`queued_runs (created_at, id) WHERE status='queued'` |
| `messages` | `id UUID PK`；`thread_id UUID NOT NULL REFERENCES threads(id)`；`run_id UUID NOT NULL`；`sequence BIGINT NOT NULL CHECK >0`；`role TEXT CHECK IN (user,assistant)`；`text TEXT NOT NULL`；`created_at`；`graph_message_id TEXT`；`UNIQUE (id, thread_id)`；`UNIQUE (thread_id, sequence)`；`UNIQUE (thread_id, graph_message_id)`；`UNIQUE (run_id, role)`；`FOREIGN KEY (run_id, thread_id) REFERENCES runs (id, thread_id) DEFERRABLE INITIALLY DEFERRED` | 由唯一约束提供 |
| `runs` 追加 | `runs_user_message_fk FOREIGN KEY (user_message_id, thread_id) REFERENCES messages (id, thread_id) DEFERRABLE INITIALLY DEFERRED` | — |
| `run_events` | `run_id UUID NOT NULL REFERENCES runs(id)`；`sequence BIGINT NOT NULL CHECK >0`；`type TEXT NOT NULL CHECK IN (run.started,phase,message.committed,state.updated,memory.retrieved,run.completed,run.failed)`；`payload JSONB NOT NULL CHECK jsonb_typeof='object'`；`created_at`；`PRIMARY KEY (run_id, sequence)` | 主键即 `(run_id, sequence)` |

延迟外键允许在同一事务中先写运行再写用户消息、或反之。

### 002：恢复与语音

| 表 | 变更 | 约束与索引 |
| --- | --- | --- |
| `runs` | `+ base_state JSONB`、`+ model_version TEXT`、`+ profile_version TEXT` | `base_state` 为私有恢复快照，不出现在 API DTO |
| `threads` | `+ recovery_run_id UUID REFERENCES runs(id)` | 指向需要检查点修复的运行 |
| `speech_jobs` | `id UUID PK`；`message_id UUID NOT NULL REFERENCES messages(id)`；`client_request_id TEXT NOT NULL CHECK length 1..200`；`status TEXT DEFAULT 'queued' CHECK IN (queued,running,completed,failed,interrupted)`；`resource_id UUID UNIQUE`；`error_code TEXT`；`created_at/finished_at`；`UNIQUE (message_id, client_request_id)` | `one_active_speech_per_message UNIQUE (message_id) WHERE status IN ('queued','running')`；`queued_speech (created_at, id) WHERE status='queued'` |

### 003：会话管理

| 表 | 变更 | 说明 |
| --- | --- | --- |
| `threads` | `+ memory_retrieval_enabled BOOLEAN NOT NULL DEFAULT true` 后 `SET DEFAULT false`；`+ memory_storage_enabled` 同样；`+ version BIGINT NOT NULL DEFAULT 1`；`+ memory_policy_version BIGINT NOT NULL DEFAULT 1`；`+ deleted_at TIMESTAMPTZ`；`+ source TEXT NOT NULL DEFAULT 'desktop' CHECK IN (desktop,cli,legacy)`；`+ import_state JSONB`；`+ history_notice TEXT` | 迁移前已有会话保留开启记忆的旧行为，新默认关闭 |
| `runs` | `+ memory_retrieval_enabled/memory_storage_enabled BOOLEAN NOT NULL DEFAULT true`；`+ memory_policy_version BIGINT NOT NULL DEFAULT 1` | 每轮运行保存策略快照 |
| `messages` | `run_id` 改为可空；`+ source TEXT NOT NULL DEFAULT 'run' CHECK IN (run,legacy)`；`+ timestamp_estimated BOOLEAN NOT NULL DEFAULT false`；`+ message_origin CHECK (run_id IS NOT NULL OR source='legacy')` | 旧 CLI 导入消息 `run_id=NULL` |
| `cli_threads` | `source_id TEXT PK`；`thread_id UUID REFERENCES threads(id) ON DELETE SET NULL`；`character_id TEXT NOT NULL`；`title TEXT NOT NULL`；`status TEXT NOT NULL CHECK IN (queued,running,completed,failed,purged)`；`checkpoint_id TEXT`；`error_code TEXT`；`created_at` | 兼作**墓碑**：已 `purged` 的 CLI 来源不得被重新发现 |
| `audio_cleanup` | `resource_id UUID PK`；`created_at` | 文件不参与数据库事务，持久化清理意图 |
| `threads` 索引 | `threads_active_created (created_at DESC, id DESC) WHERE deleted_at IS NULL` | 活动会话分页 |

### 004：检查点解除关联

| 表 | 变更 | 说明 |
| --- | --- | --- |
| `messages` | `+ detached_memory_storage_enabled BOOLEAN`、`+ detached_memory_policy_version BIGINT`；删除旧 `message_origin` 并新增：`CHECK (run_id IS NOT NULL OR source='legacy' OR (role='user' AND graph_message_id IS NOT NULL AND detached_memory_storage_enabled IS NOT NULL AND detached_memory_policy_version IS NOT NULL AND detached_memory_policy_version > 0))` | 检查点确认的用户输入在其运行被删除后仍保留身份与原记忆策略（见 [conversations 叶子](../conversations/README.md) R10.4） |

### `memory_service`（Agent 侧）

由 `MemoryJobRepository.setup()` 执行 [agent/memory/migrations/001_memory_jobs.sql](../../../agent/memory/migrations/001_memory_jobs.sql) 创建，带 `pg_advisory_xact_lock(1835363695, 2)` 串行化幂等 DDL：

| 表 | 列与约束 | 索引 |
| --- | --- | --- |
| `jobs` | `id BIGSERIAL PK`；`job_key TEXT NOT NULL UNIQUE`；`character_name TEXT NOT NULL`；`thread_id TEXT NOT NULL`；`payload JSONB NOT NULL`；`status TEXT NOT NULL DEFAULT 'pending' CHECK IN (pending,failed)`；`attempts INTEGER NOT NULL DEFAULT 0`；`next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()`；`last_error TEXT`；`created_at TIMESTAMPTZ NOT NULL DEFAULT now()`；`UNIQUE (character_name, thread_id)` | `memory_jobs_ready (next_attempt_at, id)` |
| `results` | `job_id BIGINT PK`；`job_key TEXT NOT NULL UNIQUE`；`character_name TEXT NOT NULL`；`thread_id TEXT NOT NULL`；`through_message_id TEXT NOT NULL`；`through_fingerprint TEXT NOT NULL`；`trim JSONB NOT NULL`；`completed_at TIMESTAMPTZ NOT NULL DEFAULT now()`；`acknowledged_at TIMESTAMPTZ` | `memory_results_thread (character_name, thread_id, job_id)` |

队列表只保留未成功处理的任务；成功提交时在同一事务内写 `results` 并删除 `jobs` 行（细节见 [memory/jobs 叶子](../../agent/memory/jobs/README.md)）。检查点表（`checkpoints/checkpoint_blobs/checkpoint_writes`）由 LangGraph `AsyncPostgresSaver.setup()` 维护，不在服务迁移中创建；会话永久删除时按精确线程 ID 清理（见 [conversations 叶子](../conversations/README.md) R11）。

## 分支与异常链

| 条件 | 处理 | 终点 |
| --- | --- | --- |
| 未配置 `DB_URL` | `open` 直接返回；`ping` 为 `False`；依赖数据库的路由 503 | B2/R1 |
| 连接超时或失败 | `open` 关闭池并抛出 → 服务拒绝启动 | B2 |
| 迁移目录为空 | `RuntimeError("Service migrations are missing")` | B2.1 |
| 数据库版本多于本服务 | `RuntimeError("Database service schema is newer than this server")` | B2.1 |
| 已应用迁移内容被修改 | `RuntimeError("An applied service migration has changed")` | B2.1 |
| CRLF/LF 差异 | 读取时归一，不改变校验和 | B2.1 步骤 2 |
| 并发启动多个服务 | `pg_advisory_xact_lock('mybot-ui-migrations')` 串行迁移 | B2.1 |
| 未注册角色的记忆表缺失 | `UndefinedTable` 由仓储处理，GET 不建表 | [memory 叶子](../memory/README.md) |
| 查询超时/等锁超时 | `statement_timeout=5000`、`lock_timeout=5000` → `psycopg` 异常 → 503 `database_unavailable` | [http 叶子](../http/README.md) B5 |

## 输入输出示例

`apply_migrations` 的 `schema_migrations` 行（虚构）：

| version | checksum | applied_at |
| --- | --- | --- |
| `001_service_tables` | `b2f1...64hex` | `2026-09-20T12:00:00+08:00` |
| `002_runs_and_speech` | `5a9c...64hex` | `2026-09-20T12:00:00+08:00` |
| `003_conversation_management` | `e77d...64hex` | `2026-09-21T09:00:00+08:00` |
| `004_checkpoint_detached_messages` | `91c4...64hex` | `2026-09-22T09:00:00+08:00` |

`Database.schema_version` 为 `"004_checkpoint_detached_messages"`，`GET /api/ready` 的 `schema_version` 同样返回该值。

## 关联文档与验证依据

- 上级：[服务总览](../README.md)；系统总览：[README/README.md](../../README.md)
- 同级：[conversations](../conversations/README.md)（表的主要使用方）· [memory](../memory/README.md)（`memory_service` 消费）· [speech](../speech/README.md) · [legacy](../legacy/README.md)（`cli_threads`/`audio_cleanup`）· [http](../http/README.md)（启动顺序与错误映射）· [settings](../settings/README.md)（`readiness`）
- 基础设施：[core/README.md](../../core/README.md)（Agent/CLI 的 `init_db` 与 checkpoint 连接）· [agent/memory/jobs/README.md](../../agent/memory/jobs/README.md)
- 测试依据：[tests/server/test_postgres.py](../../../tests/server/test_postgres.py)（迁移、事务、并发、队列）、[tests/server/test_api.py](../../../tests/server/test_api.py)
- 迁移自原 `server/README.md`（原文件已移除）的规则：迁移事务、锁和内容哈希保证幂等；LF/CRLF 不影响哈希；不得修改已应用迁移，应新增版本；Agent 使用现有 checkpoint 与角色记忆表，checkpoint 采用受限类型反序列化；集成测试使用独立 `mybot_api_test_<随机值>` 数据库，结束后删除，不会在业务库中执行测试迁移、改写角色记忆或 checkpoint。
- 验证记录（迁移自原文档，未在本次编写中重跑）：2026-09-21 记录 25 项独立 PostgreSQL 集成通过；2026-09-22 记录 29 项独立 PostgreSQL 集成通过。本轮仅静态核对源码与 SQL 文件。
