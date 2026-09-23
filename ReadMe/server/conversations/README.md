# 会话与运行持久化：RunRepository / ThreadRepository / CheckpointSyncService

## 职责与入口

本页覆盖 `server/repositories/runs.py`、`server/repositories/threads.py`、`server/services/checkpoint_sync.py` 以及 `server/routes/threads.py`、`server/routes/runs.py` 暴露的会话/运行语义：线程与正式历史、运行队列与事件、幂等提交、版本与回收站、记忆策略快照、检查点同步截断。

| 文件 | 类型 | 职责 |
| --- | --- | --- |
| [server/repositories/runs.py](../../../server/repositories/runs.py) | 仓储 | `RunRepository`：线程/消息/运行/事件、提交幂等、领取、终态、恢复快照 |
| [server/repositories/threads.py](../../../server/repositories/threads.py) | 仓储 | `ThreadRepository`：设置版本、回收站、检查点同步、记忆状态、永久删除 |
| [server/services/checkpoint_sync.py](../../../server/services/checkpoint_sync.py) | 服务 | `CheckpointSyncService.sync/_read`：在事务内读取最新检查点并交给仓储截断 |
| [server/routes/threads.py](../../../server/routes/threads.py) | 路由 | 会话 CRUD、消息、状态、检查点同步、记忆状态 |
| [server/routes/runs.py](../../../server/routes/runs.py) | 路由 | 提交、重试、快照、SSE（SSE 细节见 [http 叶子](../http/README.md)） |

上游：HTTP 路由与 `RunRuntime`（执行线程持有 advisory-lock 连接构造仓储）；下游：Postgres `mybot_ui` 表与 `memory_service` 表。表结构见 [persistence 叶子](../persistence/README.md)。

## 调用链总览

```text
构建阶段
B1 RunRepository.__init__ / connection() ── HTTP 用连接池；执行线程用所有权连接 + 锁
B2 ThreadRepository ── 继承 RunRepository，复用连接策略与 _thread/_run
B3 CheckpointSyncService.__init__ ── 持有 Database，sync 时新建 ThreadRepository

运行阶段
R1 RunRepository.submit ── 线程行锁内完成幂等/忙碌检查、写用户消息、运行与首个事件
R2 RunRepository.retry ── 取原用户消息文本后转 submit(retry_of=...)
R3 RunRepository.claim / context ── FOR UPDATE SKIP LOCKED 领取并组装运行上下文
R4 RunRepository.snapshot / events ── 运行快照与增量事件读取
R5 RunRepository.save_base / publish / commit_reply / finish ── 执行期间的持久化与终态
R6 RunRepository.recovery_runs / recovery_context / clear_recovery ── 启动恢复与恢复指针
R7 RunRepository.memory_messages ── 按策略版本为记忆节点提供源消息
R8 RunRepository.get_thread / list_threads / messages / create_thread ── 读取与创建
R9 ThreadRepository.update / trash ── 策略修改与回收站
R10 CheckpointSyncService.sync ── R10.1 sync_checkpoint → R10.2 _read → R10.3 _memory_fence → R10.4 _delete_tail
R11 ThreadRepository.purge ── 回收站内永久删除（含墓碑与音频清理队列）
R12 ThreadRepository.memory_status / _idle / _version / _cancel_memory ── 状态查询与公共前置校验
```

## 构建链

### B1. `RunRepository.__init__` 与 `connection()`

- 定位与签名：[server/repositories/runs.py](../../../server/repositories/runs.py) 的 `def __init__(self, pool, *, connection=None, connection_lock=None)` 与 `@asynccontextmanager async def connection(self)`。
- 调用方与条件：HTTP 路由经 `repository(request)` 构造（仅 `pool`）；执行线程用 `RunRepository(pool, connection=所有权连接, connection_lock=checkpointer.lock)`。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `pool` | `AsyncConnectionPool` | 必填 | HTTP/测试路径的连接来源 |
| `connection` | `Connection \| None` | 默认 `None` | 执行线程持有的 advisory-lock 连接；非空时所有操作固定使用它 |
| `connection_lock` | `Lock \| None` | 默认 `None` | `checkpointer.lock`，串行化运行写入与检查点写入 |

功能：`connection()` 优先让出 `worker_connection`（有锁则先加锁）；否则从 `pool.connection()` 借用。输出：异步上下文管理器。副作用：无（借用/归还连接）。

模块级常量：`TERMINAL = {"completed", "completed_with_warnings", "failed", "interrupted"}`；游标工具 `_encode(value)`（JSON → urlsafe base64 去 `=`）与 `_decode(cursor, kind, scope)`（校验 `[1, kind, scope, payload]` 形状，失败抛 400 `invalid_cursor`）。

### B2. `ThreadRepository`

- 定位与签名：[server/repositories/threads.py](../../../server/repositories/threads.py) 的 `class ThreadRepository(RunRepository)`，无自定义 `__init__`。
- 功能：复用 B1 的连接策略、`_thread/_run/_event` 与游标工具；新增会话管理、记忆状态与检查点同步。构造方式同 B1（HTTP 路由使用 `ThreadRepository(database.pool)`）。

### B3. `CheckpointSyncService.__init__`

- 定位与签名：[server/services/checkpoint_sync.py](../../../server/services/checkpoint_sync.py) 的 `def __init__(self, database)`。
- 调用方与条件：`app.py` lifespan 创建一次，挂 `app.state.checkpoint_sync`。
- 输入：`database`（`Database`，读取 `pool`）。输出：服务实例。`sync` 时才构造 `ThreadRepository(self.database.pool)`。

## 运行链

### R1. `RunRepository.submit`

- 定位与签名：`async def submit(self, thread_id, text, request_id, *, retry_of=None)`。
- 调用方与条件：`POST /api/threads/{thread_id}/runs`（`retry_of=None`）与 `retry()`（`retry_of=run_id`）。

| 输入 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `thread_id` | `UUID` | 路径 | 会话必须存在且未删除 | 行锁与归属 |
| `text` | `str` | `SubmitRun.text` | 1..50000，非空白（Pydantic 校验） | 原样入库的用户输入 |
| `request_id` | `str` | `client_request_id` | 1..200，非空白 | 幂等键 |
| `retry_of` | `UUID \| None` | `retry()` | 可选 | 关联失败运行 |

隐式输入：`hashlib.sha256(text.encode("utf-8")).hexdigest()` 作为 `payload_hash`；会话行的策略开关与 `memory_policy_version`。

功能与内部调用（单事务）：
1. `_thread(conn, thread_id, lock=True)`：`SELECT * FROM mybot_ui.threads WHERE id=%s FOR UPDATE`；不存在或已删除 → 404 `thread_not_found`。
2. 按 `(thread_id, client_request_id)` 查既有运行：
   - 存在且 `payload_hash` 与 `retry_of` 均一致 → 直接返回该行（幂等重放）。
   - 存在但内容或重试来源不同 → 409 `request_conflict`。
3. 忙碌检查：同线程存在 `status IN ('queued','running')` 的运行 → 409 `thread_busy`，`details={"run_id": ...}`。
4. 生成 `run_id`、`message_id`（`uuid4`）；取 `thread.memory_retrieval_enabled`、`thread.memory_storage_enabled`、`thread.memory_policy_version` 作为本次运行快照。
5. `retry_of` 分支：
   - `_run(conn, retry_of, lock=True)`；必须属于同一线程且状态为 `failed/interrupted`，否则 409 `retry_not_allowed`。
   - 该运行不得已有 assistant 消息（`SELECT 1 FROM mybot_ui.messages WHERE run_id=%s AND role='assistant'`）。
   - 必须是该线程最新运行（`ORDER BY created_at DESC, id DESC LIMIT 1`），否则 409 `retry_not_allowed`。
   - 复用 `previous["user_message_id"]` 与 `previous["base_state"]`；`retrieval = thread 开关 AND previous 开关`；`storage = thread 开关 AND previous 开关 AND 版本相同`。
6. 插入 `mybot_ui.runs`：`id, thread_id, user_message_id, client_request_id, payload_hash, retry_of, base_state(Jsonb 或 NULL), memory_retrieval_enabled, memory_storage_enabled, memory_policy_version`。
7. 非重试时插入用户消息：`sequence = COALESCE(MAX(sequence),0)+1`，`graph_message_id=f"user_{message_id}"`。
8. 更新 `threads.updated_at=now()`；插入首个事件 `_event(conn, run_id, "phase", {"phase":"queued"})`；`_run(conn, run_id)` 返回完整运行行。
9. 提交后记录“对话任务入队”（含 `run_id/thread_id/character/message_id/retry_of/input_chars/记忆开关`）。

输出：运行行（`status='queued'`、`phase='queued'`）。`_event` 使用 `COALESCE(MAX(sequence),0)+1` 在运行行锁内分配事件序号。异常：任一校验抛 `ServiceError` 则事务回滚，不产生半成品。

### R2. `RunRepository.retry`

- 定位与签名：`async def retry(self, run_id, request_id)`。
- 功能：`_run(conn, run_id)` 校验运行存在（线程未删除）→ 读取 `user_message_id` 对应消息文本 → 转 `submit(previous["thread_id"], text, request_id, retry_of=run_id)`。
- 输出：同 R1。约束：只有最近一次、未提交正文的 `failed/interrupted` 运行可重试；正文已提交后的警告不提供整轮重试（R1 步骤 5 校验）。

### R3. `RunRepository.claim` 与 `context`

- 定位与签名：`async def claim(self)`、`async def context(self, conn, run_id)`。
- 调用方与条件：`RunRuntime._serve` 主循环。

功能（`claim`，单事务）：
1. `SELECT * FROM mybot_ui.runs WHERE status='queued' ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED`；无行返回 `None`。
2. `UPDATE runs SET status='running', phase='preparing', started_at=now()`。
3. `_event(..., "run.started", {"phase":"preparing"})`。
4. `context = await self.context(conn, run["id"])`；记录“对话任务开始”。

`context` 的 SQL：`runs r JOIN threads t ON t.id=r.thread_id JOIN messages m ON m.id=r.user_message_id`，返回 `r.*` 加 `t.character_id`、`t.graph_thread_id`、`t.recovery_run_id`、`t.import_state`、`m.text`、`m.created_at AS input_created_at`。

输出：`context` 行（供 `AgentAdapter.execute`）。异常：无（空队列返回 `None`）。

### R4. `RunRepository.snapshot` 与 `events`

- 定位与签名：`async def snapshot(self, run_id)`、`async def events(self, run_id, after=0, limit=100)`。
- 调用方与条件：`GET /api/runs/{id}`、SSE 轮询与校验。

`snapshot`（单事务，`_run(..., lock=True)`）：
- `messages`：`WHERE id = user_message_id OR (run_id = %s AND role='assistant') ORDER BY sequence`（用户消息 + 该运行正式回复；不返回其他运行的消息）。
- `last_event_sequence`：`COALESCE(MAX(sequence),0)`。
- 返回运行行附加以上两字段；响应模型 `RunSnapshot` 会过滤私有列（如 `base_state`、`payload_hash`），私有快照不出现在 API。

`events`：先 `_run` 校验（未删除线程），再 `SELECT * FROM run_events WHERE run_id=%s AND sequence>%s ORDER BY sequence LIMIT 100`。

输出：运行快照 / 事件行列表。异常：运行或线程不存在 → 404 `run_not_found`。

### R5. 执行期写入：`save_base` / `publish` / `commit_reply` / `finish`

#### R5.1 `save_base(run_id, state, model_version, profile_version)`

`UPDATE runs SET base_state=%s, model_version=%s, profile_version=%s WHERE id=%s AND status='running'`。`state` 由 `freeze_state` 生成。条件更新保证只有运行中才写入；无返回。

#### R5.2 `publish(run_id, event_type, payload)`

- 定位与签名：`async def publish(self, run_id, event_type, payload)`。
- 调用方：`AgentAdapter` 的 `before`（`phase`）与 `astream` 投影（`state.updated`、`memory.retrieved`）。

功能（单事务）：
1. `_run(conn, run_id, lock=True)`；`status != 'running'` → 409 `run_not_active`。
2. 按类型更新：`phase` → `runs.phase=payload["phase"]`；`state.updated` → `threads.state = state || payload`（JSONB 合并，只增量覆盖投影字段）；`memory.retrieved` → `jsonb_set(state,'{retrieved_memories}', 原数组 || hits)` 追加本轮命中。
3. `_event(conn, run_id, event_type, payload)` 写事件。
4. 提交后：`phase` 记录“对话阶段变更”；`memory.retrieved` 记录“对话记忆检索完成”（含命中 ID 列表）。

输出：无返回值。事件类型与 `run_events` 的 CHECK 一致。

#### R5.3 `commit_reply(run_id, text, graph_message_id)`

- 定位与签名：`async def commit_reply(self, run_id, text, graph_message_id)`。
- 调用方：`AgentAdapter` 的 `commit` 回调（图写检查点之前）；修复路径的防御性恢复。

功能（单事务，锁顺序：先线程后运行，与 HTTP 路径一致，避免死锁）：
1. `_run`（取 `thread_id`）→ `_thread(..., lock=True)` → `_run(..., lock=True)`。
2. 已存在该运行的 assistant 消息：文本与 `graph_message_id` 完全一致 → 返回既有行；不一致 → 409 `reply_conflict`。
3. 运行非 `running` → 409 `run_not_active`。
4. `message_id = uuid5(run_id, "assistant")`（稳定 ID，重复提交天然幂等）；插入消息：`sequence = COALESCE(MAX(sequence),0)+1`、`role='assistant'`、`graph_message_id` 为图内 `reply_{run_id}`。
5. `_event(..., "message.committed", {"message": Message 的 JSON})`；更新 `threads.updated_at`。
6. 提交后记录“正式回复已提交”（完整正文与消息 ID）。

输出：消息行。顺序保证：正文与事件同事务落库成功后才返回给 `observed`，随后图才允许写检查点。

#### R5.4 `finish(run_id, *, error=None, warning=None, interrupted=False, needs_recovery=False)`

功能（单事务，先 `_run` → `_thread FOR UPDATE` → `_run FOR UPDATE`）：
1. 已是 `TERMINAL` → 直接返回（幂等）。
2. 查是否有 assistant 消息；`warnings = run.warnings` 并追加传入 `warning`。
3. 已提交正文：`error` 非空则追加 `postprocessing_interrupted`；`status = "completed_with_warnings" if warnings else "completed"`，`error=None`。
4. 未提交正文：`status = "interrupted" if interrupted else "failed"`，`error = error or "reply_failed"`。
5. `UPDATE runs SET status, phase=status, error_code, warnings(去重), finished_at=now()`。
6. `UPDATE threads SET updated_at=now(), recovery_run_id = run_id if needs_recovery else NULL`。
7. `_event(..., "run.completed" if committed else "run.failed", {"status","error_code","warnings"})`。
8. 记录“对话任务结束”。

输出：无返回值（终态落库）。边界：正文已提交后即使后处理失败也不会回退为失败；`warnings` 去重。

### R6. `recovery_runs` / `recovery_context` / `clear_recovery`

| 方法 | 签名 | 功能 | 输出 |
| --- | --- | --- | --- |
| `recovery_runs` | `async def recovery_runs(self)` | 查 `status='running'` 的运行（按 `created_at,id`），逐个 `context` | 上下文行列表（启动恢复输入） |
| `recovery_context` | `async def recovery_context(self, run_id)` | 单个 `context` | 上下文行 |
| `clear_recovery` | `async def clear_recovery(self, thread_id)` | `UPDATE threads SET recovery_run_id=NULL` | 无 |

### R7. `memory_messages`

- 定位与签名：`async def memory_messages(self, thread_id, policy_version, after=None)`。
- 调用方：`AgentAdapter.execute` 的 `memory_source` 回调。

功能：SQL 取 `mybot_ui.messages LEFT JOIN mybot_ui.runs`，条件为
`COALESCE(r.memory_storage_enabled, m.detached_memory_storage_enabled, false)` 为真、
`COALESCE(r.memory_policy_version, m.detached_memory_policy_version) = policy_version`、
且 `sequence >= (SELECT sequence FROM messages WHERE thread_id=%s AND graph_message_id=%s)`（无匹配则为 0，`after` 为空时同样从 0 开始），按 `sequence` 升序。

输出：LangChain 消息列表——`user` 用 `HumanMessage`、`assistant` 用 `AIMessage`，`id=graph_message_id`，内容为 `<timestamp>{created_at.isoformat()}</timestamp>\n` + 正文。被截断运行保留的用户消息（`run_id IS NULL`）通过 `detached_memory_*` 列参与授权判断。

### R8. 读取与创建：`create_thread` / `get_thread` / `list_threads` / `messages`

#### R8.1 `create_thread(character_id, title, *, memory_retrieval_enabled=False, memory_storage_enabled=False, source='desktop')`

插入 `threads(id, character_id, graph_thread_id, title, 记忆开关, source)`；`id=uuid4()`、`graph_thread_id=f"ui:{id}"`（迁移 001 的 CHECK 约束保证一致）。返回线程行。`source` 取值 `desktop|cli`（`legacy` 由导入服务直接插入）。

#### R8.2 `get_thread(thread_id)`

`_thread`（不含已删除）→ 查 `latest_run`（`ORDER BY created_at DESC, id DESC LIMIT 1`）与 `current_run`（`status IN ('queued','running')`），两者均为 `to_jsonb(r) - 'base_state'`。返回 `{**thread, 'current_run', 'latest_run'}`。

#### R8.3 `list_threads(*, cursor=None, limit=30, deleted=False)`

- `where`：默认 `deleted_at IS NULL`；`deleted=True` 时 `deleted_at IS NOT NULL`。
- 游标：`_decode(cursor, "threads", scope)`，`scope` 为 `all|deleted`；payload 为 `[created_at.isoformat(), id]`，必须带时区，否则 `invalid_cursor`；条件 `(t.created_at, t.id) < (%s, %s)`。
- 排序 `created_at DESC, id DESC`；每行附带 `current_run`/`latest_run` 子查询；取 `limit+1` 判断下一页；`next_cursor = _encode([1,"threads",scope,[rows[limit-1].created_at.isoformat(), str(id)]])`。
- 输出 `{"items": rows[:limit], "next_cursor": ...}`。会话列表按创建时间/ID 倒序，活跃会话更新 `updated_at` 不破坏游标。

#### R8.4 `messages(thread_id, *, before=None, limit=30)`

`_thread` 校验 → `before` 游标（`_decode(..., "messages", str(thread_id))`，payload 为 `sequence`，必须 0<seq≤2^63-1）→ `sequence < before` → `ORDER BY sequence DESC LIMIT limit+1` → `items = reversed(rows[:limit])`（按 sequence 升序返回）→ `next_cursor` 使用 `rows[limit-1].sequence` 供读取更早消息。

### R9. `ThreadRepository.update` 与 `trash`

#### R9.1 `update(thread_id, version, changes)`

- 输入：`changes` 由路由从 `UpdateThread` 去掉 `expected_version` 与 `None` 字段得到（`title`、`memory_retrieval_enabled`、`memory_storage_enabled`）。
- 功能（单事务）：
  1. `_thread(..., lock=True)`；`_version`（`thread.version != version` → 409 `thread_conflict`）。
  2. `_idle`：存在 `queued/running` 运行，或该线程消息存在 `queued/running` 语音任务 → 409 `thread_busy`。
  3. 计算最终 `title`（`changes.get('title') or thread['title']`）与两个开关（未指定保持原值）；`changed = 开关是否变化`。
  4. `changed` 时 `_cancel_memory`：删除该角色/图线程在 `memory_service.jobs` 的未完成任务（表存在时）。
  5. `UPDATE threads SET title, 两个开关, memory_policy_version = memory_policy_version + changed, version = version + 1, updated_at=now()`。
- 输出：更新后的线程行。语义：`version` 随每次成功修改 +1；`memory_policy_version` 仅在策略变化时 +1。

#### R9.2 `trash(thread_id, version, *, restore=False)`

1. `_thread(..., lock=True, include_deleted=True)`；`_version`；`_idle`。
2. 非恢复时 `_cancel_memory`。
3. `UPDATE threads SET deleted_at = CASE WHEN restore THEN NULL ELSE now() END, memory_policy_version+1, version+1, updated_at=now()`。
4. 输出：更新后的线程行。回收站中的会话：`GET /threads/{id}` 与消息/资源接口因 `_thread`/`_run` 过滤而 404；`GET /threads?deleted=true` 可列出；`POST restore` 恢复后继续访问。

### R10. 检查点同步（`CheckpointSyncService.sync` 与 `ThreadRepository.sync_checkpoint`）

#### R10.1 `CheckpointSyncService.sync(thread_id, body)`

- 定位与签名：[server/services/checkpoint_sync.py](../../../server/services/checkpoint_sync.py) 的 `async def sync(self, thread_id, body)`。
- 输入：`body` 为 `CheckpointSync`（`expected_version`、`dry_run`、`checkpoint_id`）。
- 功能：`database.pool is None` → 503 `database_unavailable`；`repository = ThreadRepository(database.pool)`；`await repository.sync_checkpoint(thread_id, body.expected_version, dry_run=body.dry_run, checkpoint_id=body.checkpoint_id, read=self._read)`；记录“检查点同步”（含删除消息/运行数）。返回结果 dict。

#### R10.2 `ThreadRepository.sync_checkpoint(thread_id, version, *, dry_run, checkpoint_id, read)`

- 定位与签名：[server/repositories/threads.py](../../../server/repositories/threads.py) 的 `async def sync_checkpoint(...)`。

功能（单事务）：
1. `_thread(..., lock=True)`；`_version`；`_idle`（活动运行或语音 → 409 `thread_busy`）。
2. `source = await read(conn, thread)`：在行锁内读取检查点（R10.2.1）。
3. 提交模式（`dry_run=False`）且 `checkpoint_id != source['checkpoint_id']` → 409 `checkpoint_changed`（预览后检查点变化）。
4. 读取 UI 全部消息（按 sequence）；取检查点最新可确认消息 `latest = source['messages'][-1]`：
   - `latest` 非空：按稳定 `graph_message_id == latest['source_message_id']` 从后往前找匹配；找不到 → 409 `checkpoint_conflict`，`details` 附 `latest_message_id`、`checkpoint_tail`（最近 5 条源 ID）、`ui_tail`（最近 5 条 UI 消息摘要）。
   - `latest` 为空且存在 UI 消息 → 409 `checkpoint_empty`。
   - `latest` 为空且无 UI 消息：不删除任何内容。
5. `extra = sequence > match.sequence 的 UI 行`（截断尾部）；`await self._memory_fence(...)`（R10.3）。
6. 计算待删运行：`extra` 行的 `run_id` 集合，加上 `runs.user_message_id IN extra_ids` 的运行 ID。
7. 构造结果：`dry_run`、`checkpoint_id`、`latest_message_id/text`、`matched_message_id`、`delete_count`、`delete_preview`（最多 20 条 `Message`）、`preview_truncated`、`run_count`、`version`（当前）、`state`（检查点公开状态投影）。
8. `dry_run or not extra_ids` → 直接返回（无修改成功时版本不增加）。
9. `await self._delete_tail(...)`（R10.4）；`UPDATE threads SET state = state || source.state, version = version + 1, updated_at=now(), history_notice = LEFT(COALESCE(history_notice || ' ', '') || 提示, 1000)`；结果中的 `version` 更新为自增后的值。
10. 输出：`CheckpointSyncResult` 对应 dict。

语义：只截断尾部；因上下文裁剪而不存在于检查点的更早消息保留，不重建全部历史；阶段 1 不修改检查点，也不回滚长期记忆。

##### R10.2.1 `CheckpointSyncService._read(conn, thread)`

- 在调用方事务内构造 `AsyncPostgresSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=None))`，`aget_tuple({"configurable": {"thread_id": thread["graph_thread_id"], "checkpoint_ns": ""}})`。
- 无检查点且无 `thread["import_state"]` → 409 `checkpoint_missing`；有导入种子时用 `thaw_state(import_state)`，`checkpoint_id=None`，时间戳取 `datetime.now(timezone.utc)`。
- 有检查点：`values = value.checkpoint["channel_values"]`，`checkpoint_id = value.checkpoint["id"]`，`stamp = fromisoformat(value.checkpoint["ts"])`。
- `checkpoint_messages(values, stamp, ai_prefixes=("reply_", "legacy_"))`（见 [legacy 叶子](../legacy/README.md)）得到可确认消息。
- 返回 `{checkpoint_id, messages, state: public_state(values), markers}`；`markers` 含 `processed`（`memory_processed_through`）、`submitted`（`memory_submitted_through`）、`pending`（`memory_pending_job.through_message_id`）、`active`（`bool(memory_active_job)`）。

#### R10.3 `_memory_fence(conn, thread, source, rows, match, extra)`

- 无待删消息（`extra` 为空）→ 直接返回。
- 检查点 `markers.active` 或 `markers.pending` 为真 → 409 `memory_busy`（“检查点仍在处理记忆任务”）。
- `memory_service.jobs` 存在该角色/图线程任务 → 409 `memory_busy`，`details={"job_id": ...}`。
- `processed`/`submitted` 标记存在但不在“保留消息”（`sequence <= match.sequence` 的 `graph_message_id` 集合）中 → 409 `memory_busy`，`details={"marker": name}`。
- 目的：检查点存在待处理/进行中记忆任务，或记忆进度指向将被删除的消息时拒绝截断。

#### R10.4 `_delete_tail(conn, thread_id, extra_ids, run_ids)`

按顺序：
1. `INSERT INTO mybot_ui.audio_cleanup(resource_id) SELECT COALESCE(s.resource_id, s.id) FROM speech_jobs s WHERE s.message_id = ANY(extra_ids) ON CONFLICT DO NOTHING`；`DELETE FROM speech_jobs WHERE message_id = ANY(extra_ids)`。
2. `run_ids` 非空时：
   - 清空指向待删运行的 `threads.recovery_run_id`；
   - `UPDATE messages SET run_id=NULL, detached_memory_storage_enabled=r.memory_storage_enabled, detached_memory_policy_version=r.memory_policy_version FROM runs r WHERE m.run_id=r.id AND r.id=ANY(run_ids) AND m.id<>ALL(extra_ids) AND m.role='user'`（检查点确认的用户输入保留身份与原记忆策略）；
   - `DELETE run_events`、`UPDATE runs SET retry_of=NULL`（指向待删运行的引用）、`DELETE runs`。
3. `DELETE messages WHERE id = ANY(extra_ids)`。
- 输出：无。副作用：音频文件由 [legacy 叶子](../legacy/README.md) 的清理循环按队列删除。

### R11. `ThreadRepository.purge`

- 定位与签名：`async def purge(self, thread_id, version)`。
- 调用方：`DELETE /api/threads/{thread_id}/purge`（返回 `{"deleted": True}`）。

功能（单事务）：
1. `_thread(..., lock=True, include_deleted=True)`；`_version`；`deleted_at is None` → 409 `trash_required`；`_idle`；`_cancel_memory`。
2. `SET CONSTRAINTS ALL DEFERRED` 后：
   - 将线程内所有语音任务资源写入 `audio_cleanup`（`COALESCE(resource_id, id)`），清空 `threads.recovery_run_id`；
   - `UPDATE cli_threads SET status='purged' WHERE thread_id=%s RETURNING source_id`；并 `INSERT cli_threads(source_id=str(thread_id), ..., 'purged') ON CONFLICT DO NOTHING`，阻止 CLI 解析把已删 UUID 重新建为来源；
   - 对检查点三张表（`checkpoint_writes`、`checkpoint_blobs`、`checkpoints`，存在时按 `graph_thread_id` 与来源 ID 精确删除）；
   - 删除 `memory_service.results`（该角色 + 图线程/来源）；
   - 依次删除 `speech_jobs`、`run_events`、`messages`、`runs`、`threads`。
- 输出：无返回值。语义：角色长期记忆不随会话删除；保留 CLI 来源墓碑，阻止已删除历史再次导入。

### R12. `memory_status` 与公共校验

#### R12.1 `memory_status(thread_id, active_job=None)`

- 调用方：`GET /api/threads/{thread_id}/memory-status`（`active_job` 取 `runtime.memory.active_job`）。

功能：
1. `_thread` 校验；`memory_storage_enabled` 为假 → `{"status":"disabled"}`。
2. 查 `to_regclass('memory_service.jobs')` / `to_regclass('memory_service.results')`：
   - `jobs` 存在且有该角色/图线程任务行：`running` 仅当 `active_job` 的 `id/thread_id/character_name` 与任务一致；否则返回任务自身状态（`pending` 或 `failed`），并附 `job_id`。
   - 否则 `results` 存在且有记录 → `{"status":"completed","job_id": 最新 job_id}`。
   - 否则 `{"status":"idle"}`。

状态含义：`disabled`=会话关闭存储；`idle`=无任务无结果；`pending`=队列等待（独立进程 Worker 执行中也显示 `pending`）；`running`=本服务记忆线程正在处理；`failed`=任务失败；`completed`=最近一项结果已提交。响应模型 `MemoryStatus`。

#### R12.2 `_idle(conn, thread_id)`

`EXISTS`（同线程 `queued/running` 运行）`OR EXISTS`（线程消息存在 `queued/running` 语音任务）→ 409 `thread_busy`（“请等待本会话的对话或语音任务结束”）。

#### R12.3 `_version(thread, version)`

`thread["version"] != version` → 409 `thread_conflict`（“会话设置已变化，请重新读取后再操作”）。

#### R12.4 `_cancel_memory(conn, thread)`

`memory_service.jobs` 表存在时 `DELETE ... WHERE character_name=%s AND thread_id=%s`。调用点：策略变更（R9.1）、移入回收站（R9.2 非恢复）、永久删除（R11）。

### R13. 路由行为汇总

| 接口 | 请求与行为 | 主要错误码 |
| --- | --- | --- |
| `GET /api/threads` | `cursor/limit(1..100,默认30)/deleted` | `invalid_cursor` 400 |
| `POST /api/threads` | `CreateThread`；先 `catalog.get(character_id)` | `character_not_found` 404 |
| `GET /api/threads/{id}` | 线程 + `version` + `current_run/latest_run` | `thread_not_found` 404 |
| `PATCH /api/threads/{id}` | 至少一个可变字段，否则 `empty_update`；`expected_version` 必填 | `empty_update` 400、`thread_busy` 409、`thread_conflict` 409 |
| `DELETE /api/threads/{id}` | 移入回收站 | `thread_busy`、`thread_conflict` |
| `GET /api/threads?deleted=true` | 回收站分页 | `invalid_cursor` |
| `POST /api/threads/{id}/restore` | 恢复历史与原策略 | 同上 |
| `POST /api/threads/{id}/checkpoint-sync` | `dry_run` 预览；提交携带预览 `checkpoint_id` | `checkpoint_missing/empty/conflict/changed`、`memory_busy`、`thread_busy`、`thread_conflict` |
| `DELETE /api/threads/{id}/purge` | 仅回收站会话 | `trash_required`、`thread_busy`、`thread_conflict` |
| `GET /api/threads/{id}/messages` | `before/limit` 向前分页 | `thread_not_found`、`invalid_cursor` |
| `GET /api/threads/{id}/state` | 返回 `thread["state"]` | `thread_not_found` |
| `GET /api/threads/{id}/memory-status` | `status/job_id` | `thread_not_found` |
| `POST /api/threads/{id}/runs` | `text/client_request_id`；先 `require_runtime` | `runtime_unavailable` 503、`request_conflict` 409、`thread_busy` 409 |
| `POST /api/runs/{id}/retry` | 新请求键；复用原用户消息 | `retry_not_allowed`、`request_conflict`、`run_not_found` |
| `GET /api/runs/{id}` | 快照（状态、正式消息、版本、最后事件序号） | `run_not_found` |
| `GET /api/runs/{id}/events` | SSE（见 [http 叶子](../http/README.md)） | `invalid_event_id`、`run_not_found` |

## 分支与异常链

| 条件 | 处理 | 终点 |
| --- | --- | --- |
| 相同请求键 + 相同内容 | 返回原运行，不重复生成 | R1 步骤 2 |
| 相同请求键 + 不同内容或不同重试来源 | 409 `request_conflict` | R1 |
| 同线程已有未完成运行 | 409 `thread_busy`（附 `run_id`） | R1/R12.2 |
| 重试非最近失败运行 / 已提交正文 | 409 `retry_not_allowed` | R1 步骤 5 |
| 版本不符 | 409 `thread_conflict` | R9/R10/R11 |
| 回收站会话接受新输入或资源读取 | `_thread`/`_run` 过滤 → 404 | R9.2 |
| 未先入回收站就永久删除 | 409 `trash_required` | R11 |
| 检查点在预览后变化 | 409 `checkpoint_changed` | R10.2 步骤 3 |
| 检查点最新消息不在 UI 历史 | 409 `checkpoint_conflict`（附尾部对比） | R10.2 步骤 4 |
| 检查点无可确认消息但 UI 有消息 | 409 `checkpoint_empty` | R10.2 步骤 4 |
| 无检查点且无导入种子 | 409 `checkpoint_missing` | R10.2.1 |
| 记忆任务进行中/进度指向待删消息 | 409 `memory_busy` | R10.3 |
| 预览匹配且无待删消息 | 成功返回，`version` 不变 | R10.2 步骤 8 |
| 检查点确认用户输入而运行被删 | 消息保留（`run_id=NULL` + `detached_*` 策略），不创建替代运行 | R10.4 |
| 运行提交后进程中断 | `recovery_runs` + `repair` + `finish(interrupted)` | 见 [runtime 叶子](../runtime/README.md) |

## 输入输出示例

R1 提交（虚构 ID）：

```json
// 请求
{"text": "晚上好。", "client_request_id": "req-0001"}
// 返回 202
{"run_id": "3f8c1c2e-...", "status": "queued"}
```

R1 入库增量：`runs` 一行（`status=queued`、`phase=queued`、`payload_hash=sha256("晚上好。")`、策略快照）、`messages` 一行（`role=user`、`sequence=1`、`graph_message_id=user_<message_id>`）、`run_events` 一行（`phase: queued`）。

R10 预览结果（有 2 条待删消息）：

```json
{"dry_run": true, "checkpoint_id": "1f0a...", "latest_message_id": "reply_3f8c...",
 "matched_message_id": "b1c2...", "delete_count": 2,
 "delete_preview": [{"id": "...", "role": "assistant", "sequence": 5, "text": "..."}],
 "preview_truncated": false, "run_count": 1, "version": 7,
 "state": {"character_state": {"mood": "平静"}}}
```

R5.4 终态判定：

| 场景 | `committed` | 传入 error | 结果 status / warnings |
| --- | --- | --- | --- |
| 正常完成 | 是 | `None` | `completed` |
| 正文已提交、记忆后处理失败 | 是 | `memory_delayed`（warning） | `completed_with_warnings` / `["memory_delayed"]` |
| 未提交正文、Agent 异常 | 否 | `agent_failed` | `failed` / `error_code=agent_failed` |
| 关闭取消、未提交正文 | 否 | `service_interrupted` | `interrupted` |

## 关联文档与验证依据

- 上级：[服务总览](../README.md)；系统总览：[README/README.md](../../README.md)
- 同级：[runtime](../runtime/README.md)（领取与调用方）· [persistence](../persistence/README.md)（表结构与约束）· [legacy](../legacy/README.md)（`checkpoint_messages`、导入与墓碑）· [memory](../memory/README.md)（记忆状态语义）· [http](../http/README.md)（路由与 SSE）
- Agent 侧：[memory/jobs/README.md](../../agent/memory/jobs/README.md) · [memory/policy/README.md](../../agent/memory/policy/README.md) · [builder/README.md](../../agent/builder/README.md)
- 测试依据：[tests/server/test_conversations.py](../../../tests/server/test_conversations.py)、[tests/server/test_postgres.py](../../../tests/server/test_postgres.py)、[tests/server/test_api.py](../../../tests/server/test_api.py)
- 迁移自原 `server/README.md`（原文件已移除）的规则：文本最长 50000 字符、空白校验不改写原文；正式角色消息仅来自检查通过的 `commit_reply`；消息包含 `id/thread_id/run_id/sequence/role/text/created_at`；历史每页按 sequence 升序返回；新建/导入会话默认关闭记忆，003 迁移前已有会话保留开启行为；未指定 PATCH 字段保持原值；`memory_policy_version` 仅随策略/删除状态变化；重新开启不补存关闭期间消息。
- 验证记录（迁移自原文档，未在本次编写中重跑）：2026-09-21 记录 25 项独立 PostgreSQL 集成通过，覆盖事务/并发、SSE 重放与提交两侧故障恢复；会话管理更新验收覆盖四种记忆策略、关闭后再开启的消息范围、提交前撤权检查、导入回溯/去重/中断恢复、回收站及资源清理。
