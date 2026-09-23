# memory.jobs（持久化队列与结果回执）

## 职责与入口

- 所属类别：PostgreSQL 持久化仓储（不是图节点）。
- 源码：[agent/memory/jobs.py](../../../../agent/memory/jobs.py)
- 迁移：[agent/memory/migrations/001_memory_jobs.sql](../../../../agent/memory/migrations/001_memory_jobs.sql)
- 调用方：图节点 `enqueue_memory`/`apply_memory_results`（[../../node/memory/README.md](../../node/memory/README.md)）、`MemoryWorker`（[../worker/README.md](../worker/README.md)）、服务端 `ThreadRepository.memory_status`。

## 表结构

| 表 | 关键列 | 说明 |
| --- | --- | --- |
| `memory_service.jobs` | `id`、`job_key UNIQUE`、`character_name`、`thread_id`、`payload JSONB`、`status`（`pending`/`failed`）、`attempts`、`next_attempt_at`、`last_error`、`UNIQUE(character_name, thread_id)` | 只保留未成功处理的任务 |
| `memory_service.results` | `job_id PK`、`job_key UNIQUE`、`character_name`、`thread_id`、`through_message_id`、`through_fingerprint`、`trim JSONB`（`remove_ids`+`fingerprints`）、`completed_at`、`acknowledged_at` | 裁剪交接与永久幂等回执 |

## 调用链总览

| 阶段 | 步骤 | 关系 |
| --- | --- | --- |
| 构建 | B1 `MemoryJobRepository.create` → `setup` | 建 schema/表（幂等迁移） |
| 运行 | R1 `enqueue(payload)` | 图节点投递（幂等、单任务） |
| 运行 | R2 `worker_session()` / `next_job(conn)` | Worker 领取 |
| 运行 | R3 `finish(conn, job, store, plan)` | 原子提交 |
| 运行 | R4 `fail(conn, job, error, max_attempts)` | 失败退避 |
| 运行 | R5 `results_after` / `acknowledge` / `job_status` / `retry` | 结果查询与维护 |

## 构建链

### B1. `create` / `setup`

- 定位与签名：`MemoryJobRepository.create(pool)` 异步工厂（[agent/memory/jobs.py:26](../../../../agent/memory/jobs.py#L26)）；`setup()`（[agent/memory/jobs.py:32](../../../../agent/memory/jobs.py#L32)）。
- 行为：读取 `migrations/001_memory_jobs.sql`，先取 `pg_advisory_xact_lock(1835363695, 2)` 串行化 DDL（CLI 与 Worker 可能同时启动），再在事务中执行幂等建表语句。
- 输出：仓储实例。异常：迁移文件缺失或 SQL 失败向上抛出，调用方无法启动消费者。

## 运行链

### R1. `enqueue`

- 定位与签名：`async def enqueue(self, payload: dict) -> int`，[agent/memory/jobs.py:44](../../../../agent/memory/jobs.py#L44)。
- 调用方：图节点 `enqueue_memory`；返回任务 ID。

| 输入字段 | 类型 | 来源 | 用途 |
| --- | --- | --- | --- |
| `payload["job_key"]` | `str` | 快照 sha256 | 幂等键 |
| `payload["character_name"]`/`["thread_id"]` | `str` | 快照 | 会话唯一约束与流锁 |
| `payload["through_message_id"]` | `str` | 快照 | 日志 |

功能与内部调用：

1. 开启事务；`memory_permitted(conn, payload, lock=True)` 不通过 → 抛 `MemoryBusyError('该会话已撤销此记忆任务')`（[../policy/README.md](../policy/README.md)）。
2. `_lock_stream(conn, character_name, thread_id)`：按 `memory-stream:{角色}:{线程}` 取事务级 advisory lock，串行化同会话投递。
3. 若 `results` 中已有同 `job_key` 回执 → 直接返回原 `job_id`（重复投递幂等）。
4. 若 `jobs` 中已有同 `(character_name, thread_id)` 任务：`job_key` 不同 → 抛 `MemoryBusyError("该会话还有未完成的记忆任务")`；相同 → 返回原 ID。
5. 插入 `jobs` 并返回新 ID；提交后记录“记忆任务入队”日志。

异常与边界：任何异常回滚事务；`MemoryBusyError` 由 `_enqueue_pending` 捕获为 warning，快照保留待补投。

### R2. `worker_session` / `next_job`

- `worker_session()`：[agent/memory/jobs.py:75](../../../../agent/memory/jobs.py#L75)。用 `pg_try_advisory_lock(1835363695, 1)` 保证同一数据库只有一个消费者；未取到锁时 `yield None`，`finally` 释放。进程/连接断开自动释放，不使用 `running` 租约。
- `next_job(conn)`：[agent/memory/jobs.py:87](../../../../agent/memory/jobs.py#L87)。短事务中领取：

```sql
SELECT j.* FROM memory_service.jobs j
WHERE j.status = 'pending' AND j.next_attempt_at <= now()
  AND NOT EXISTS (SELECT 1 FROM memory_service.jobs older
                  WHERE older.character_name = j.character_name AND older.id < j.id)
ORDER BY j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED
```

- 语义：较老的 `failed`/退避任务阻止同角色后续任务越过；其他角色不受影响；任务领取后仍留在表中，成功提交时才删除。

### R3. `finish`

- 定位与签名：`async def finish(self, conn, job: dict, store, plan) -> bool`，[agent/memory/jobs.py:102](../../../../agent/memory/jobs.py#L102)。
- 调用方：`MemoryWorker._process`，必须传入持有消费者锁的同一连接。

功能（单事务）：

1. `register_vector_async(conn)` 注册 pgvector 类型。
2. 再次 `memory_permitted(..., lock=True)`：不通过则删除任务行并返回 `False`（撤销）。
3. `_lock_stream` 锁定会话流；`SELECT ... FOR UPDATE` 确认任务仍存在，否则抛 `RuntimeError("提交时记忆任务不存在")`。
4. `store.apply_operations(conn, plan.operations, changes=changes)` 执行记忆增删改（[../store/README.md](../store/README.md)）。
5. 计算快照每条消息的 `message_fingerprint`，插入 `results` 行：`through_message_id`、末尾消息指纹、`trim={"remove_ids", "fingerprints"}`。
6. 删除 `jobs` 行；提交后逐条记录已提交的记忆变更日志。
- 输出：`True` 表示提交成功；`False` 表示提交前被撤销（任务已删除）。

异常：SQL/存储异常抛出，事务回滚，任务仍在队列中等待退避重试。

### R4. `fail`

- 定位与签名：`async def fail(self, conn, job, error, max_attempts) -> None`，[agent/memory/jobs.py:136](../../../../agent/memory/jobs.py#L136)。
- 行为：`attempts = job["attempts"] + 1`；达到 `max_attempts` 置 `failed`，否则保持 `pending`；`next_attempt_at = now() + min(300, 2^min(attempts,8)) 秒`；记录 `last_error=type(error).__name__`；`log_failure` 保留调用栈与重试信息。
- 边界：只统计被捕获的失败；硬崩溃不消耗次数，重启后从仍在表中的任务重新计算。

### R5. 结果查询与维护

| 方法 | 签名 | 行为 |
| --- | --- | --- |
| `results_after` | `(character_name, thread_id, after_id) -> list[dict]` | 返回 `job_id > after_id` 的结果，按 `job_id` 升序；把 `trim` 的 `remove_ids`/`fingerprints` 展开到行上 |
| `acknowledge` | `(character_name, thread_id, through_id)` | 将 `job_id <= through_id` 且未确认的结果标记 `acknowledged_at=now()` |
| `job_status` | `(job_id) -> str \| None` | 返回 `pending`/`failed`；任务已删除返回 `None` |
| `retry` | `(job_id) -> bool` | 仅将 `failed` 任务重置为 `pending`、`attempts=0`、清除错误与退避；成功记录日志 |

## 分支与异常链

- **重复投递**：同 `job_key` 返回原任务或原结果 ID，不重复计算。
- **同会话已有任务**：`job_key` 不同时拒绝新任务，避免重叠；图侧据此保留快照待补投。
- **提交前撤权**：删除任务并返回 `False`，不写记忆、不写结果。
- **失败退避**：指数退避上限 300 秒；达到上限后需手动 `--retry`。
- **多 Worker/多服务**：数据库锁保证同一时刻只有一个消费者；其他进程轮询等待。

## 输入输出示例

适用 R3：

```text
输入：job={id:42, job_key:"ab12...", character_name:"SuLi", thread_id:"ui:...",
           payload:{...}},
      plan=MemoryPlan(operations=[insert...], remove_ids=["m1","m2"])
输出：True；jobs 行删除；results 行 (job_id=42, through_message_id=..., trim={...})
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 消费者：[../worker/README.md](../worker/README.md) · 计算：[../processor/README.md](../processor/README.md) · 权限：[../policy/README.md](../policy/README.md)
- 图内交接：[../../node/memory/README.md](../../node/memory/README.md) · 快照指纹：[../../utils/memory/README.md](../../utils/memory/README.md)
- 依据：`agent/memory/jobs.py` 与 `migrations/001_memory_jobs.sql`；`tests/test_memory_service.py`、`tests/server/test_memory_runtime.py` 覆盖幂等入队、提交与失败保留；本次未执行测试。
