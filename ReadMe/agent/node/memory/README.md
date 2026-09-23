# memory 节点（图内记忆交接）

## 职责与入口

- 所属类别：图节点工厂 ×3，负责图与后台记忆之间的交接，不做记忆计算。
- 源码：[agent/node/memory.py](../../../../agent/node/memory.py)
- 图注册名与实例（[agent/builder.py](../../../../agent/builder.py)）：

| 图注册名 | 工厂 | 触发位置 |
| --- | --- | --- |
| `prepare_memory` | `create_prepare_memory_node(character_name, memory_source)` | `tts` 条件边返回 `True` 后 |
| `enqueue_memory` | `create_enqueue_memory_node(memory_jobs)` | `prepare_memory` 固定边 |
| `apply_memory_results` | `create_apply_memory_results_node(memory_jobs, character_name)` | 轮初 `begin_turn` 固定边 |

- 异步边界：`enqueue_memory` 只把快照写入队列即返回；记忆计算由独立 Worker（[../../memory/worker/README.md](../../memory/worker/README.md)）执行，结果在后续轮次由 `apply_memory_results` 应用。

## 调用链总览

| 阶段 | 步骤 | 关系 |
| --- | --- | --- |
| 构建 | B1 `create_prepare_memory_node` | 工厂（可选 `memory_source` 包装） |
| 构建 | B2 `create_enqueue_memory_node` | 工厂 |
| 构建 | B3 `create_apply_memory_results_node` | 工厂 |
| 运行 | R1 `prepare` / `managed_prepare` | 图调度：生成快照 |
| 运行 | R2 `enqueue` → `_enqueue_pending` | 图调度：入队 |
| 运行 | R3 `apply_results` | 图调度：轮初同步结果 |
| 运行 | R3.1 `jobs.results_after` / `acknowledge` / `job_status` | 仓储调用 |
| 运行 | R3.2 `freeze_messages` / `message_fingerprint` / `result_removals` | 裁剪安全校验（[../../utils/memory/README.md](../../utils/memory/README.md)） |

## 构建链

### B1. `create_prepare_memory_node(character_name, source=None)`

- 定位与签名：同步工厂，[agent/node/memory.py:25](../../../../agent/node/memory.py#L25)。
- 输入：`character_name`（写入快照）；`source` 为可选的异步消息来源函数 `async (state) -> list`。
- 输出：`source is None` 时返回 `prepare(state, config)`；否则返回 `managed_prepare(state, config)`。
- `managed_prepare` 先调用 `source(state)` 取得受管会话的正式消息档案，再用 `{**state, 'messages': messages}` 调用 `prepare`；只让授权的正式消息进入记忆模型。
- 副作用：无；构建期不访问数据库。

### B2. `create_enqueue_memory_node(jobs)`

- 定位与签名：同步工厂，返回 `async def enqueue(state)`，[agent/node/memory.py:71](../../../../agent/node/memory.py#L71)。
- 输入：`jobs` 为 `MemoryJobRepository` 或 `None`。
- 输出：执行函数，内部委托 `_enqueue_pending`。

### B3. `create_apply_memory_results_node(jobs, character_name)`

- 定位与签名：同步工厂，返回 `async def apply_results(state, config)`，[agent/node/memory.py:77](../../../../agent/node/memory.py#L77)。
- 输入：`jobs`、`character_name`。
- 输出：执行函数。

## 运行链

### R1. `prepare` / `managed_prepare`

- 定位与签名：`create_prepare_memory_node.<locals>.prepare(state, config)`（同步）与 `managed_prepare(state, config)`（异步），[agent/node/memory.py:26](../../../../agent/node/memory.py#L26)。
- 调用方与条件：`tts` 的 `event_judge` 返回 `True` 时调度。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `memory_storage_enabled` | `bool` | 服务端每轮输入 | 缺省 `True` | 关闭时返回 `{}` |
| `memory_pending_job` / `memory_active_job` | 各类型 | checkpoint | 非空时返回 `{}` | 避免重叠任务 |
| `messages` | `list` | checkpoint 或 `source` | 需全部有唯一稳定 ID | 生成快照 |
| `memory_processed_through` / `memory_processed_fingerprint` | `str` | checkpoint | 可空 | 计算新增消息起点 |
| `turn_id` | `str` | `begin_turn` | 必填 | 快照标识 |
| `iteration` | `int` | `update_iter` | 缺省 `0` | 入队后扣减 |
| `world_state` | `dict` | `world_state_update` | 可空 | 快照上下文 |

隐式输入：LangGraph `RunnableConfig.configurable.thread_id`（`_thread_id` 校验，缺失抛 `ValueError`）。

功能与内部调用：

1. 存储关闭或已有任务 → `{}`。
2. `build_memory_payload(state, character_name, thread_id)`（[../../utils/memory/README.md](../../utils/memory/README.md)）：
   - `freeze_messages` 要求每条消息有唯一 ID，否则 `ValueError`；
   - 有 `memory_processed_through` 时校验边界消息存在且指纹一致，否则抛错；
   - 生成 `version=1` 快照，含 `messages`、`new_message_start`、`through_message_id`、`job_key` 等；
   - 受管会话（state 含 `memory_policy_version`）额外写入策略字段。
3. 捕获 `ValueError`/`TypeError`/`KeyError` → 返回 `{"memory_warning": "记忆快照边界无效，保留原文和处理进度，本轮回复已完成。"}`（不投递、不阻塞）。
4. 成功返回 `{"memory_pending_job": payload}`；payload 为 `None`（没有新消息）时返回 `{}`。
5. `managed_prepare` 在 `source(state)` 抛错时不捕获，向上抛出由服务端处理。

### R2. `enqueue` → `_enqueue_pending`

- 定位与签名：`create_enqueue_memory_node.<locals>.enqueue(state)`，异步，[agent/node/memory.py:72](../../../../agent/node/memory.py#L72)；内部 `_enqueue_pending(state, jobs)`，[agent/node/memory.py:50](../../../../agent/node/memory.py#L50)。
- 调用方与条件：`prepare_memory` 固定边。

功能：

1. 存储关闭或 `memory_pending_job` 为空 → `{}`。
2. `jobs is None` → `{"memory_warning": "记忆队列不可用，投递快照已保留，稍后补投。"}`。
3. `await jobs.enqueue(payload)`（[../../memory/jobs/README.md](../../memory/jobs/README.md)）：按 `job_key` 幂等；同线程已有不同任务时抛 `MemoryBusyError`。
4. 成功返回：

| 输出字段 | 类型 | 含义 |
| --- | --- | --- |
| `memory_pending_job` | `None` | 快照已交出 |
| `memory_active_job` | `int` | 新任务 ID |
| `memory_submitted_through` | `str` | 已投递到的消息 ID |
| `iteration` | `int` | `max(0, iteration - payload["iteration"])`，扣减已入记忆的轮数 |
| `memory_warning` | `""` | 清除告警 |

5. 入队异常捕获为 warning（“记忆任务尚未入队，快照已保留，下一轮补投。”），不改变已提交回复。

### R3. `apply_results`

- 定位与签名：`create_apply_memory_results_node.<locals>.apply_results(state, config)`，异步，[agent/node/memory.py:78](../../../../agent/node/memory.py#L78)。
- 调用方与条件：`begin_turn` 固定边，每轮最先执行；处理顺序为“确认上一轮已应用的结果 → 补投未入队快照 → 读取并应用新完成的结果”。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `memory_storage_enabled` | `bool` | 服务端输入 | 关闭时 `{}` | 策略 |
| `memory_pending_job` | `dict` | 上轮 | 可空 | 补投 |
| `memory_active_job` | `int` | 上轮 | 可空 | 结果同步与失败提示 |
| `memory_last_applied_job` | `int` | checkpoint | 缺省 `0` | 已确认结果水位 |
| `memory_processed_through`/`memory_processed_fingerprint` | `str` | checkpoint | 可空 | 进度推进 |
| `messages` | `list` | checkpoint | 用于安全裁剪校验 | 生成 `RemoveMessage` |
| `memory_policy_version` | `int` | 服务端输入 | 存在表示受管会话 | 选择同步路径 |

功能与内部调用：

1. 守卫：存储关闭、`jobs is None`、无 `thread_id` 任一满足 → `{}`。
2. 若 `last_applied` 非空，先 `await jobs.acknowledge(character_name, thread_id, last_applied)`（R3.1）：确认**旧**水位的结果，避免在返回本次 `RemoveMessage` 前清除本次结果。
3. `updates.update(await _enqueue_pending(state, jobs))`：补投上一轮未入队的快照。
4. `results = await jobs.results_after(character_name, thread_id, last_applied)`（R3.1）：取所有新完成结果。
5. 逐条处理：
   - **受管会话**（state 含 `memory_policy_version`）：只推进 `memory_last_applied_job`/`memory_processed_through`/指纹并清 warning；上下文裁剪独立进行，不做消息删除。若 `active` 已完成则清空。
   - **旧路径**：用 `freeze_messages`/`message_fingerprint` 校验 `through_message_id` 与快照指纹（R3.2）；历史变化或裁剪边界不再安全时**跳过本次裁剪但仍确认结果**，记录 `memory_warning` 继续同步后续结果；安全时调用 `result_removals` 生成 `RemoveMessage` 列表并更新 `memory_trimmed_through`。
6. 若 `active` 任务状态为 `failed`（`jobs.job_status`），写“重试耗尽，可单独重试”的 warning。
7. 整个流程异常捕获为 `memory_warning="记忆结果暂时无法同步，继续使用已有对话。"`，不改变本轮对话。

| 输出或状态字段 | 类型 | 产生条件 | 含义 | 消费方 |
| --- | --- | --- | --- | --- |
| `messages` | `list[RemoveMessage]` | 旧路径且裁剪安全 | 删除已完成整理的历史消息 | `limit_context`、checkpoint |
| `memory_last_applied_job` | `int` | 每条结果处理后 | 已确认水位 | 下一轮 acknowledge |
| `memory_processed_through` / `memory_processed_fingerprint` | `str` | 每条结果处理后 | 整理进度边界与指纹 | `build_memory_payload` |
| `memory_active_job` | `None` | 活动任务已完成 | 允许新任务 | `event_judge` |
| `memory_pending_job` | 各类型 | 补投后 | 清空或保留 | `enqueue_memory` |
| `memory_warning` | `str` | 异常/跳过/失败时 | 后台异常提示（不是 `reply_error`） | 服务端运行 warning、前端提示 |

副作用：数据库查询/更新（acknowledge、results、job_status）；日志。

异常与边界：除显式守卫外，整个 R3 包在 `try/except Exception` 中，失败只写 warning；结果已由 Worker 原子提交，跳过裁剪不影响记忆写入。

## 分支与异常链

- **快照边界无效**：`prepare` 返回 warning，原文与进度保留，本轮正常结束。
- **队列不可用/入队失败**：快照保留在 `memory_pending_job`，下一轮 `apply_results` 补投。
- **历史被上下文裁剪**：旧路径可能找不到 `through_message_id`；此时跳过裁剪、确认结果，避免阻塞后续同步。
- **任务失败**：`job_status == "failed"` 时只提示，可 `python -m agent.memory.worker --retry <id>` 重试。
- **受管会话**：裁剪由 `limit_context` 与记忆进度独立处理，`apply_results` 不返回 `RemoveMessage`。

## 输入输出示例

适用 R2（成功入队）：

```text
输入：memory_pending_job={... "through_message_id":"msg_9", "iteration":5},
      iteration=5, memory_active_job=None
输出：{"memory_pending_job":None, "memory_active_job":42,
       "memory_submitted_through":"msg_9", "iteration":0, "memory_warning":""}
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 后台：[../../memory/README.md](../../memory/README.md)
- 快照工具：[../../utils/memory/README.md](../../utils/memory/README.md) · 队列：[../../memory/jobs/README.md](../../memory/jobs/README.md) · 协议字段：[../../classes/state/README.md](../../classes/state/README.md)
- 依据：`agent/node/memory.py`；`tests/test_reply_memory.py`、`tests/test_memory_service.py`、`tests/server/test_memory_runtime.py` 覆盖交接与补投；本次未执行测试。
