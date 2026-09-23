# memory.worker（独立记忆消费者）

## 职责与入口

- 所属类别：后台任务消费者（不是图节点，也不属于请求调用链）。
- 源码：[agent/memory/worker.py](../../../../agent/memory/worker.py)
- 命令行入口：`python -m agent.memory.worker [--once] [--poll-interval] [--max-attempts] [--retry ID]`。
- 服务内使用：`server/services/memory_runtime.py` 的 `MemoryRuntime` 在同一进程的独立线程/事件循环中构造并运行 `MemoryWorker`。
- 触发方式：轮询数据库队列，不使用请求触发。

## 调用链总览

| 阶段 | 步骤 | 关系 |
| --- | --- | --- |
| 构建 | B1 `MemoryWorker.__init__` | 构造消费者（注入 jobs/store 工厂/处理器） |
| 运行 | R1 `run_once()` | 领取并处理一个任务 |
| 运行 | R1.1 `worker_session` / `next_job` / `memory_permitted` | 仓储与权限调用 |
| 运行 | R2 `_process(conn, job)` | 计算与提交 |
| 运行 | R2.1 `store_factory` / `write_session` / `processor` | 存储与计划 |
| 运行 | R2.2 `jobs.finish` / `jobs.fail` | 原子提交或失败退避 |
| 入口 | E1 `run_worker(...)` / `parse_args()` | 独立进程循环 |

## 构建链

### B1. `MemoryWorker.__init__`

- 定位与签名：`MemoryWorker(jobs, *, store_factory=None, processor=process_memory_snapshot, max_attempts=5)`，同步构造，[agent/memory/worker.py:19](../../../../agent/memory/worker.py#L19)。

| 输入 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `jobs` | `MemoryJobRepository` | 必填 | 队列仓储 |
| `store_factory` | 异步工厂 | `AsyncPostgresCharacterMemoryStore.create` | 按角色创建存储实例 |
| `processor` | 异步函数 | `process_memory_snapshot` | 快照计算入口 |
| `max_attempts` | `int` | `5` | 失败重试上限，达到后状态置 `failed` |

- 状态：`active_job` 记录当前任务摘要（供服务端状态投影），处理结束清空。

## 运行链

### R1. `run_once`

- 定位与签名：`async def run_once(self) -> bool`，[agent/memory/worker.py:26](../../../../agent/memory/worker.py#L26)。
- 输出：`True` 表示本轮领取并处理了一个任务（或撤销任务）；`False` 表示没有可执行任务或已有其他 Worker 持有锁。

功能与内部调用：

1. `async with self.jobs.worker_session() as conn`：尝试获取数据库级消费者锁；`conn is None`（其他进程在消费）→ 返回 `False`。
2. `job = await self.jobs.next_job(conn)`：领取最老的 `pending` 且到期的任务；同角色更老的任务会阻止后续任务越过（[jobs/README.md](../jobs/README.md)）；无任务 → `False`。
3. `memory_permitted(conn, job['payload'])`（[policy/README.md](../policy/README.md)）：策略不再允许时删除任务行并记录“记忆任务已撤销”，返回 `True`。
4. 在 `logging_context(job_id, thread_id, character)` 中调用 `_process(conn, job)`。

### R2. `_process`

- 定位与签名：`async def _process(self, conn, job)`，[agent/memory/worker.py:43](../../../../agent/memory/worker.py#L43)。

功能与内部调用：

1. 记录 `active_job` 与“记忆任务开始”日志（含尝试次数）。
2. `store = await self.store_factory(self.jobs.pool, job["character_name"])`（R2.1）。
3. `async with store.write_session(conn)`：持有角色级会话锁覆盖检索/计算/提交（锁内不持有长写事务）。
4. `plan = await self.processor(job["payload"], store)`：计算增删改计划与待裁剪消息（[processor/README.md](../processor/README.md)）。
5. `committed = await self.jobs.finish(conn, job, store, plan)`（R2.2）：写记忆、写结果、删队列行在同一事务提交；返回 `False` 表示提交前权限核对失败（任务已撤销）。
6. 成功记录“记忆任务完成”（操作数、裁剪数、耗时）；`False` 记录“记忆任务已撤销”。
7. 异常处理：
   - `asyncio.CancelledError`：记录“记忆任务中断，等待恢复”并重新抛出（进程停止场景）；
   - 其他异常：`await self.jobs.fail(conn, job, exc, self.max_attempts)` 记录失败类型并按退避重排；
   - `finally` 清空 `active_job`。

| 输出 | 类型 | 产生条件 | 含义 | 消费方 |
| --- | --- | --- | --- | --- |
| 返回值 | `bool` | 总是 | 是否处理了任务 | `run_worker` 轮询节奏 |
| 队列/结果/记忆表 | 数据库变更 | `finish` 成功 | 记忆提交、结果回执、任务删除 | 图节点、服务端状态 |

副作用：模型调用、数据库事务、日志。异常与边界：硬崩溃不消耗重试次数，任务留在表中由重启后的 Worker 重新计算；原生推理不可安全中断，取消只保证不再开始新任务。

## 入口

### E1. `run_worker` 与 `parse_args`

- `run_worker(*, once=False, poll_interval=1.0, max_attempts=5, retry=None)`：[agent/memory/worker.py:69](../../../../agent/memory/worker.py#L69)。
  1. `await core.db.init_db(checkpoints=False)` 初始化连接池（不建 checkpoint 表）。
  2. `jobs = await MemoryJobRepository.create(core.db.pool)`（会执行幂等迁移）。
  3. `retry` 非空时先调用 `jobs.retry(retry)`；任务不存在或不是 `failed` 抛 `ValueError`。
  4. 循环 `worker.run_once()`；数据库异常记录后继续（`--once` 时抛出）；空闲时 `sleep(poll_interval)`。
  5. `finally` 关闭数据库。
- `parse_args()`：[agent/memory/worker.py:92](../../../../agent/memory/worker.py#L92)。校验 `poll-interval > 0`、`max-attempts >= 1`。
- `__main__`：`asyncio.run(...)`；`KeyboardInterrupt` 静默退出。
- 模块顶部导入 `core.db`：Windows 下事件循环策略必须在 `asyncio.run` 创建循环前安装。

## 分支与异常链

- **其他 Worker 持有锁**：`run_once` 返回 `False`，等待下一次轮询；不会重复计算当前任务。
- **权限撤销**：处理前与提交前各核对一次；撤销时删除任务行，不写记忆。
- **失败退避**：`jobs.fail` 按 `min(300, 2^attempts)` 秒重排，达到 `max_attempts` 后置 `failed`；同角色后续任务被阻止，其他角色仍可执行。
- **手动重试**：`python -m agent.memory.worker --retry 123` 将 `failed` 任务重置为 `pending`。
- **进程中断**：`CancelledError` 只记录并抛出，任务保留；`--once` 用于测试单步执行。

## 输入输出示例

```text
$ python -m agent.memory.worker --once
（领取 job_id=42 → 计算 → 事务提交 → 记录“记忆任务完成”）
（无任务时直接退出）
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 队列：[../jobs/README.md](../jobs/README.md) · 计算：[../processor/README.md](../processor/README.md) · 权限：[../policy/README.md](../policy/README.md) · 存储：[../store/README.md](../store/README.md)
- 服务内消费者：[server/services/memory_runtime.py](../../../../server/services/memory_runtime.py)
- 依据：`agent/memory/worker.py`；`tests/test_memory_service.py`、`tests/server/test_memory_runtime.py` 覆盖单任务、失败保留与重启消费；本次未执行测试。
