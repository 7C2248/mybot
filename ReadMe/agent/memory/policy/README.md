# memory.policy（记忆权限核对）

## 职责与入口

- 所属类别：数据库权限核对函数（不是图节点）。
- 源码：[agent/memory/policy.py](../../../../agent/memory/policy.py)
- 调用方：`MemoryJobRepository.enqueue`、`MemoryJobRepository.finish`（`lock=True`）与 `MemoryWorker.run_once`（[../worker/README.md](../worker/README.md)）。
- 目的：受管会话在入队、计算前和写事务提交前三次核对策略，关闭存储或删除会话后不产生追溯授权。

## 调用链总览

| 步骤 | 函数 | 关系 |
| --- | --- | --- |
| R1 | `memory_permitted(conn, payload, *, lock=False)` | 队列/Worker 直接调用 |
| R1.1 | `pg_advisory_xact_lock(memory-stream:...)` | `lock=True` 时锁定会话流 |

## 运行链

### R1. `memory_permitted`

- 定位与签名：`async def memory_permitted(conn, payload, *, lock=False) -> bool`，[agent/memory/policy.py:4](../../../../agent/memory/policy.py#L4)。

| 输入 | 类型 | 来源 | 含义 |
| --- | --- | --- | --- |
| `conn` | psycopg 连接 | 调用方事务/连接 | 执行查询与加锁 |
| `payload["thread_id"]` | `str` | 快照 | 会话标识；`ui:` 前缀表示受管会话 |
| `payload["character_name"]` | `str` | 快照 | 必须与线程绑定的角色一致 |
| `payload["memory_policy_version"]` | `int` | 快照 | 必须与线程当前策略版本一致（缺省 1） |
| `lock` | `bool` | 默认 `False` | 为真时对会话流取事务级 advisory lock，串行化入队/提交 |

功能：

1. **非受管流**（`thread_id` 不以 `ui:` 开头，即旧 CLI 独立流）：
   - `lock=True` 时取 `memory-stream:{角色}:{线程}` 事务锁；
   - 若 `mybot_ui.cli_threads` 表不存在 → 允许（旧环境未启用会话管理）；
   - 若该流已被登记为导入别名（`cli_threads.source_id = thread_id`）→ 拒绝：旧独立流在关联导入后停止接收任务，后续统一使用受管会话 ID。
2. **受管会话**（`ui:` 前缀）：
   - `mybot_ui.threads` 表不存在 → 拒绝；
   - 查询线程行（`lock=True` 时 `FOR UPDATE`），要求：存在、`deleted_at IS NULL`、`memory_storage_enabled` 为真、`character_id` 与快照一致、`memory_policy_version` 与快照一致；任一不满足返回 `False`。

| 输出 | 类型 | 产生条件 | 含义 | 消费方 |
| --- | --- | --- | --- | --- |
| 布尔值 | `bool` | 总是 | `True` 允许入队/提交；`False` 表示已撤销或不允许 | `enqueue`（抛 `MemoryBusyError`）、`finish`（删任务返回 `False`）、`run_once`（删任务跳过） |

副作用：`lock=True` 时获取事务级锁；查询 `mybot_ui` 表。异常与边界：数据库异常向上抛出，由调用方按失败处理；该函数不修改任何数据。

## 分支与异常链

- **关闭存储后重开**：`memory_policy_version` 递增使旧快照不再匹配；未完成任务在入队/计算/提交任一环节被撤销。
- **删除会话**：`deleted_at` 非空即拒绝；回收站中的任务被清理（见 `ThreadRepository`）。
- **导入旧 CLI**：登记别名后旧流任务被拒绝，避免旧标识与新会话重复整理。
- **独立 Worker**：同样调用本函数，因此托管策略对独立进程生效。

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 队列：[../jobs/README.md](../jobs/README.md) · 消费者：[../worker/README.md](../worker/README.md)
- 服务端策略：[server/README.md](../../../server/README.md)（记忆策略与版本）
- 依据：`agent/memory/policy.py`；`tests/test_memory_service.py`、`tests/server/test_memory_runtime.py` 覆盖撤权与版本核对；本次未执行测试。
