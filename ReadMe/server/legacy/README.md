# 旧 CLI 历史导入：LegacyService

## 职责与入口

本页覆盖 `server/services/legacy.py` 与 `server/routes/legacy.py`：只读发现旧 CLI 检查点、预览可确认消息、把历史导入为受管理的图线程（`source='legacy'`），以及回收站永久删除后的墓碑与音频清理队列。

| 文件 | 类型 | 职责 |
| --- | --- | --- |
| [server/services/legacy.py](../../../server/services/legacy.py) | 服务 | `LegacyService`：发现、排队导入、回溯父链、落库、清理音频 |
| [server/routes/legacy.py](../../../server/routes/legacy.py) | 路由 | `/api/legacy/*` 与 `/api/cli/resolve` |
| [server/repositories/threads.py](../../../server/repositories/threads.py) | 协作 | 墓碑判定与永久删除写入 `cli_threads`（见 [conversations 叶子](../conversations/README.md)） |

上游：`app.py` lifespan（`LegacyService(database, catalog, audio_root)`，`start()` 创建后台循环）；迁移自原 `server/README.md` 的记录（未在本轮核对前端源码）：桌面端启动时自动发现，并为角色明确的会话提交导入。下游：Postgres `checkpoints` 表（LangGraph）、`mybot_ui.cli_threads`、`mybot_ui.threads/messages`、`memory_service.jobs`。

## 调用链总览

```text
构建阶段
B1 LegacyService.__init__ ── 保存 database/catalog/audio_root，初始化任务与失败标记
B2 LegacyService.start / close ── 创建/取消后台导入循环任务

运行阶段
R1 LegacyService._loop ── 每 0.5s：advisory lock → _cleanup_audio → 领取 queued/running 任务 → _import
R2 LegacyService._cleanup_audio ── 按 audio_cleanup 队列删除 WAV 文件
R3 LegacyService.discover ── 列出检查点与导入状态，做角色证据推断
R4 LegacyService.request_import ── 幂等/墓碑/角色校验后置 queued，返回 job DTO
R5 LegacyService.status / job_dto ── 查询导入状态
R6 LegacyService.resolve ── CLI 解析：复用桌面 UUID、复用别名、新建共享会话或转导入
R7 LegacyService.preview ── 读取最新检查点，预览最多 100 条可确认消息
R8 LegacyService._import ── 回溯检查点父链、去重、建线程与消息、写墓碑别名完成
辅助：source_key / checkpoint_messages / _latest
```

关系：`request_import` 是生产者（写 `cli_threads.status='queued'` 后返回），`_loop` 是消费者（轮询领取并执行 `_import`）；二者通过数据库队列交接，不共享内存状态。

## 构建链

### B1. `LegacyService.__init__`

- 定位与签名：[server/services/legacy.py](../../../server/services/legacy.py) 的 `def __init__(self, database, catalog, audio_root)`（同步）。
- 输入：`database`（`Database`）、`catalog`（`CharacterCatalog`）、`audio_root`（`settings.audio_root`）。
- 功能：保存三个依赖；`self.task=None`；`self._queue_failed=False`（队列不可用只记录首次异常）；`self._audio_failures=set()`（音频清理失败去重日志）。
- 属性 `pool`：`database.pool is None` 时抛 503 `database_unavailable`，否则返回连接池。

### B2. `start` 与 `close`

| 方法 | 签名 | 功能 |
| --- | --- | --- |
| `start` | `async def start(self)` | 仅当 `database.pool is not None` 时 `asyncio.create_task(self._loop())` |
| `close` | `async def close(self)` | `task.cancel()`；`with suppress(asyncio.CancelledError): await self.task` |

输出：无返回值。副作用：后台任务生命周期跟随 FastAPI lifespan。

## 运行链

### R1. `_loop`：导入循环与中断恢复

- 定位与签名：`async def _loop(self)`。
- 调用方与条件：`start()` 创建，持续运行直到关闭。

每轮（循环末尾 `await asyncio.sleep(0.5)`）：
1. 借连接池连接；`pg_try_advisory_lock(hashtextextended('mybot-legacy-import', 0))`：同一数据库只允许一个导入循环。
2. 取得锁后：
   - `await self._cleanup_audio(conn)`（R2）。
   - `SELECT * FROM mybot_ui.cli_threads WHERE status IN ('queued','running') ORDER BY created_at LIMIT 1`：**重启后遗留的 `running` 任务会被重新领取**，即中断导入可恢复。
   - 有任务：`UPDATE cli_threads SET status='running'` → 记录“旧历史导入开始” → `await self._import(conn, job)`。
   - `_import` 抛异常：`UPDATE cli_threads SET status='failed', error_code=%s`，错误码取 `ServiceError.code` 或 `'legacy_unreadable'`，记录“旧历史导入失败”。
   - `finally`：连接未关闭则 `pg_advisory_unlock('mybot-legacy-import')`。
3. 连接/队列异常：仅首次记录“历史导入队列不可用”，置 `_queue_failed=True`；恢复后记录“历史导入队列已恢复”。

输出：无返回值。副作用：导入任务状态迁移与日志。

### R2. `_cleanup_audio`

- 定位与签名：`async def _cleanup_audio(self, conn)`。
- 输入：`audio_cleanup` 队列最多 50 行。
- 功能：逐行 `audio_path(self.audio_root, resource_id)` → `await asyncio.to_thread(path.unlink, missing_ok=True)` → 删除队列行 → 记录“音频资源清理完成”；`OSError` 时保留队列行，且仅首次为该 ID 记录“音频清理失败，保留待办”。
- 输出：无。语义：文件系统不参与数据库事务，因此用持久队列保证最终清理。

### R3. `discover`

- 定位与签名：`async def discover(self, *, cursor=None, limit=30)`。
- 调用方：`GET /api/legacy/threads`（`cursor` 为上一页最后 `source_id`，≤255 字符）。

功能：
1. `checkpoints` 表不存在 → 返回 `{"items": [], "next_cursor": null}`。
2. 查询：`SELECT DISTINCT ON (c.thread_id) c.thread_id AS source_id, c.checkpoint_id, c.metadata, a.status, a.character_id, a.thread_id, a.error_code FROM checkpoints c LEFT JOIN mybot_ui.cli_threads a ON a.source_id=c.thread_id WHERE c.checkpoint_ns='' AND c.thread_id NOT LIKE 'ui:%' AND (a.status IS NULL OR a.status IN ('queued','running','failed')) AND (%s::text IS NULL OR c.thread_id > %s) ORDER BY c.thread_id, c.checkpoint_id DESC LIMIT limit+1`。
3. 角色证据推断（`character_id` 为空时）：`metadata.character_name`；或 `memory_service.jobs/results` 中该 `thread_id` 的唯一 `character_name`；唯一候选且 `catalog.get` 成功才采纳，否则保持 `None`。
4. 每项输出 `{source_id, thread_id, error_code, character_id, status: alias.status or 'unlinked', title: f"CLI · {source_id}"}`；`next_cursor` 为第 `limit` 项的 `source_id`（存在更多时）。

输出：`{"items": [...], "next_cursor": ...}`。边界：已 `completed/purged` 的别名不出现在发现列表（墓碑阻止再次导入）。

### R4. `request_import`

- 定位与签名：`async def request_import(self, source, character, title)`。
- 调用方：`POST /api/legacy/threads/{source_id}/import`。

功能（单事务）：
1. `source_key(source)`（辅助函数）：空值、>255、以 `ui:` 开头或含 `/`、`\`、`\x00` → 400 `invalid_cli_thread`；`catalog.get(character)` 校验角色。
2. `pg_advisory_xact_lock(hashtextextended('cli-source:{source}', 0))`：同一来源串行。
3. 既有 `cli_threads` 行（`FOR UPDATE`）：
   - `status='purged'` → 410 `thread_purged`（“该 CLI 会话已永久删除，请新建会话”）。
   - `character_id != character` → 409 `character_conflict`。
   - `status != 'failed'` → 直接返回 `job_dto(existing)`（幂等；`queued/running/completed` 均复用）。
4. `latest = await self._latest(conn, source)`（最新非空 `checkpoint_ns` 检查点）；无 → 404 `legacy_not_found`。
5. `pg_advisory_xact_lock('memory-stream:{character}:{source}')` 后 upsert：`INSERT ... VALUES (..., 'queued', checkpoint_id) ON CONFLICT (source_id) DO UPDATE SET status='queued', error_code=NULL, checkpoint_id=EXCLUDED.checkpoint_id`。
6. `memory_service.jobs` 存在时删除该角色/来源的旧独立记忆流任务（注册导入即退役旧流）。
7. 返回 `job_dto(row)`。

输出：`{source_id, thread_id, character_id, status, error_code}`（`thread_id` 此时可能为 `None`）。异常：校验失败即回滚。

### R5. `status` 与 `job_dto`

- `status(source)`：`SELECT * FROM cli_threads WHERE source_id=%s`；无行 → 404 `legacy_not_found`；否则 `job_dto(row)`。
- `job_dto(row)`（静态方法）：只取 `source_id, thread_id, character_id, status, error_code` 五个字段。

### R6. `resolve`：CLI 共用会话

- 定位与签名：`async def resolve(self, source, character, title, retrieval=False, storage=False)`。
- 调用方：`POST /api/cli/resolve`（请求体 `ResolveCLI`：`source_id/character_id/title` 与两个记忆开关）。

功能（单事务，`cli-source` 锁）：
1. `source_key` + `catalog.get`。
2. 若 `source` 可解析为 `UUID` 且 `mybot_ui.threads` 存在该 ID：
   - 已删除 → 409 `thread_deleted`；
   - 否则直接返回 `{source_id, thread_id, character_id, status:'completed', error_code:None}`（复用桌面会话）。
3. 否则查 `cli_threads` 别名：`purged` → 410 `thread_purged`；角色不符 → 409 `character_conflict`；其余返回 `job_dto`。
4. 无别名且 `_latest` 无检查点：`RunRepository(self.pool, connection=conn).create_thread(character, title, memory_retrieval_enabled=retrieval, memory_storage_enabled=storage, source='cli')` 新建共享会话，并插入 `cli_threads` 别名（`status='completed'`），返回 `job_dto`。
5. 有检查点：事务提交后转 `request_import(source, character, title)`（注意：新建共享会话的开关不会带入导入，导入会话默认关闭记忆）。

输出：job DTO 或 `thread_deleted`/`thread_purged`/`character_conflict` 错误。

### R7. `preview`

- 定位与签名：`async def preview(self, source)`。
- 调用方：`GET /api/legacy/threads/{source_id}/messages`。

功能：`source_key` → `_latest`（无 → 404 `legacy_not_found`）→ 别名 `purged` → 410 `thread_purged` → `AsyncPostgresSaver(conn)` 读取最新元组 → `checkpoint_messages(value.checkpoint['channel_values'], fromisoformat(ts))` → 返回 `{"items": items[-100:], "notice": "仅预览当前保留的消息；导入时会回溯可用检查点。" + 无法确认提示}`。

输出：最多 100 条预览项（字段见 `checkpoint_messages`）。预览不执行图、不写库。

### R8. `_import`：回溯父链、去重与落库

- 定位与签名：`async def _import(self, conn, job)`。
- 调用方：`_loop` 领取到任务后，在同一连接上执行。

功能与内部调用：
1. `AsyncPostgresSaver(conn)`；`config` 指向 `job['source_id']` 与 `job['checkpoint_id']`。
2. 用 `build_rp_agent(job['character_id'], checkpointer=saver, recovery_only=True)` 构建只读图，`snapshot = await graph.aget_state(config)`；`snapshot.next` 非空 → 409 `legacy_interrupted`（“旧 CLI 会话存在未完成节点，需先核对后再导入”），不自动执行旧任务。
3. 从指定检查点沿 `parent_config` 回溯父链：
   - 重复访问同一 `checkpoint_id` → `ValueError('Cyclic checkpoint ancestry')`；
   - `aget_tuple` 返回 `None` → `missing=True` 并停止；
   - 每层 `checkpoint_messages(value.checkpoint['channel_values'], fromisoformat(ts))` 收集可确认项，`omitted` 累加。
4. 去重：按 `source_message_id` 合并（父链从旧到新遍历，后出现的快照覆盖同 ID 项；若新快照时间为估算值则保留首次观测时间）。
5. 空集 → 409 `legacy_empty`。
6. 组装提示 `notice`（检查点不是完整档案；含“未纳入无法确认的旧回复”“部分父检查点已缺失”条件语句）。
7. 生成消息：`identifier = uuid5(NAMESPACE_URL, f"mybot-cli:{job['source_id']}:{item['source_message_id']}")`，`graph_message_id = f'legacy_{identifier}'`；角色 `user`/`assistant`、原文、时间与 `timestamp_estimated`。只有仍存在于最新检查点消息集合（`retained`）的项才进入 `import_state` 上下文种子。
8. 公开状态 `state = public_state(latest_values)`；`seed = freeze_state({**state, 'messages': context, 'iteration': 0, 'memory_policy_version': 1, 'memory_retrieval_enabled': False, 'memory_storage_enabled': False})`。
9. 单事务：再次 `_latest` 校验 `checkpoint_id` 未变化（否则 409 `legacy_changed`，“导入期间 CLI 历史发生变化，请停止旧 CLI 后重试”）→ 插入 `threads`（`source='legacy'`、`graph_thread_id=f'ui:{thread_id}'`、`state`、`import_state`、`history_notice`）→ `executemany` 插入全部消息（`source='legacy'`）→ `UPDATE cli_threads SET thread_id=%s, status='completed', error_code=NULL`。
10. 记录“旧历史导入完成”（含消息数）。

输出：无返回值。副作用：新线程/消息/别名完成状态；`import_state` 供首次运行在图内 `aupdate_state(..., as_node='reply_failed')` 恢复上下文（见 [runtime 叶子](../runtime/README.md) R4）。

### R9. 辅助函数

| 名称 | 签名 | 功能 |
| --- | --- | --- |
| `source_key` | `def source_key(value)` | 校验 CLI 来源串；返回原值或抛 400 `invalid_cli_thread` |
| `checkpoint_messages` | `def checkpoint_messages(values, stamp, ai_prefixes=('reply_',))` | 从检查点 `channel_values.messages` 提取“可确认”的用户输入与正式回复 |
| `LegacyService._latest` | `async def _latest(self, conn, source)` | `checkpoints` 表存在时取 `checkpoint_ns=''` 的最新行（`ORDER BY checkpoint_id DESC LIMIT 1`），否则 `None` |

`checkpoint_messages` 规则（被 preview、_import、checkpoint_sync 共用）：
- 只处理 `HumanMessage`/`AIMessage`；其他类型跳过。
- AI 消息带 `tool_calls` 或 id 不以 `ai_prefixes`（`reply_`，检查点同步另加 `legacy_`）开头 → 跳过；不带工具调用时置 `omitted=True`（“无法确认的 AI 文本”）。
- 无 id 或 `content` 非字符串 → 跳过并置 `omitted=True`。
- 解析内容开头的 `<timestamp>...</timestamp>\r?\n?`：可解析且带时区时使用该时间且 `timestamp_estimated=False`；否则使用传入 `stamp` 且 `timestamp_estimated=True`；文本去掉前缀。
- 输出项：`source_message_id`、`role`、`text`、`created_at`、`timestamp_estimated`；返回 `(items, omitted)`。

## 分支与异常链

| 条件 | 处理 | 终点 |
| --- | --- | --- |
| 来源串非法 | 400 `invalid_cli_thread` | R4/R6/R7 前置 |
| 别名已 `purged`（墓碑） | 410 `thread_purged` | R4/R6/R7 |
| 别名角色不符 | 409 `character_conflict` | R4/R6 |
| 别名已 queued/running/completed | 直接返回既有 DTO（幂等） | R4 |
| 无检查点 | 404 `legacy_not_found` | R4/R7；R6 改为新建共享会话 |
| 检查点存在未完成节点 | 409 `legacy_interrupted`，不自动执行旧任务 | R8 步骤 2 |
| 导入期间检查点变化 | 409 `legacy_changed`，事务回滚 | R8 步骤 9 |
| 父链环 | `ValueError('Cyclic checkpoint ancestry')` → 任务 `failed` | R8/R1 |
| 无可确认消息 | 409 `legacy_empty` | R8 步骤 5 |
| 导入过程异常 | `cli_threads.status='failed'` + 错误码；重启后循环重新领取 `running` 任务 | R1 |
| 服务重启时任务为 `running` | 直接重新领取执行 | R1 |
| 音频删除失败 | 队列行保留，日志去重 | R2 |
| `memory_service.jobs` 存在旧独立流 | 注册导入时删除 | R4 步骤 6 |
| 永久删除会话 | `purge` 写 `cli_threads` 墓碑（含 `source_id=str(thread_id)`） | [conversations 叶子](../conversations/README.md) R11 |

## 输入输出示例

发现（R3）：

```json
{"items": [{"source_id": "cli-2026-09-01", "thread_id": null, "character_id": "小满",
            "status": "unlinked", "error_code": null, "title": "CLI · cli-2026-09-01"}],
 "next_cursor": null}
```

导入请求（R4）：

```http
POST /api/legacy/threads/cli-2026-09-01/import
{"character_id": "小满", "title": "CLI 历史"}
```

```json
{"source_id": "cli-2026-09-01", "thread_id": null, "character_id": "小满", "status": "queued", "error_code": null}
```

预览项（R7/R9，虚构 ID）：

```json
{"source_message_id": "user_1a2b...", "role": "user", "text": "早上好。",
 "created_at": "2026-09-01T08:00:00+08:00", "timestamp_estimated": false}
```

## 关联文档与验证依据

- 上级：[服务总览](../README.md)；系统总览：[README/README.md](../../README.md)
- 同级：[conversations](../conversations/README.md)（`cli_threads` 墓碑与永久删除）· [runtime](../runtime/README.md)（`import_state` 使用）· [persistence](../persistence/README.md)（`cli_threads`/`audio_cleanup` 表）· [speech](../speech/README.md)（音频文件路径）
- Agent 侧：[builder/README.md](../../agent/builder/README.md)（`recovery_only=True`）· [memory/jobs/README.md](../../agent/memory/jobs/README.md) · [memory/policy/README.md](../../agent/memory/policy/README.md)（CLI 独立流退役规则）
- CLI 侧：[cli/README.md](../../cli/README.md)
- 测试依据：[tests/server/test_conversations.py](../../../tests/server/test_conversations.py)（导入回溯/去重/中断恢复/墓碑）、[tests/test_conversation_policy.py](../../../tests/test_conversation_policy.py)
- 迁移自原 `server/README.md`（原文件已移除）的规则：旧 CLI 通过稳定消息 ID 回溯当前检查点的父链，纳入用户输入和 `reply_` 正式回复，排除草稿、工具结果和无法确认的 AI 文本；去重并保留原文；缺少可信时间时标记 `timestamp_estimated`；导入消息 `source=legacy`、`run_id=null`；检查点并非完整档案，界面提示可能有缺失；角色证据来自明确元数据或唯一角色的记忆队列/结果记录，证据不足由用户关联；前端启动时自动发现，并为角色明确的会话提交导入。
- 验证记录（迁移自原文档，未在本次编写中重跑）：会话管理更新验收覆盖导入回溯/去重/中断恢复与手工角色关联；本轮仅静态核对源码。
