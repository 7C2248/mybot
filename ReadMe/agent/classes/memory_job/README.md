# PreparedMemory / MemoryOperation / MemoryPlan（后台记忆计算产物）

## 职责与入口

- 所属类别：后台数据产物（`agent/classes/memory_job.py`，`@dataclass`），不是图节点，也不是 Pydantic 协议；文件说明明确“不包含数据库连接或可变图状态”。
- 源码：[`memory_job.py`](../../../../agent/classes/memory_job.py)
- 生产方：[`agent/memory/processor.py`](../../../../agent/memory/processor.py) 的 `process_memory_snapshot`、[`agent/memory/store.py`](../../../../agent/memory/store.py) 的 `prepare_memory` 与直接 CRUD 方法。
- 消费方：[`agent/memory/worker.py`](../../../../agent/memory/worker.py) 的 `MemoryWorker._process`、[`agent/memory/jobs.py`](../../../../agent/memory/jobs.py) 的 `MemoryJobRepository.finish`、[`agent/memory/store.py`](../../../../agent/memory/store.py) 的 `apply_operations`；测试用它们构造替身计划。

## 定义

### `PreparedMemory`

```python
@dataclass
class PreparedMemory:
    text: str
    importance: int | None
    event_date: str | None
    keywords: str | None
    chunks: list[tuple[str, Any]]
```

| 字段 | 类型 | 约束 | 语义 |
| --- | --- | --- | --- |
| `text` | `str` | 无默认值 | 父记忆正文，写入父表 `memory` 列 |
| `importance` | `int \| None` | 可为 `None` | 重要度（0~100）；插入时 `data.importance or 0` 归一为 0 |
| `event_date` | `str \| None` | `yyyy-mm-dd` 或 `None` | 事件日期；调用方未显式提供时由 `chunk_memory` 自动提取 |
| `keywords` | `str \| None` | 可为 `None` | 关键词串；同上自动提取 |
| `chunks` | `list[tuple[str, Any]]` | 非空（`prepare_memory` 强制） | `(chunk_text, embedding)` 列表；向量类型由编码器决定（`Any`），写入子表 `embedding vector(1024)` |

### `MemoryOperation`

```python
@dataclass
class MemoryOperation:
    name: str
    memory_id: int | None = None
    prepared: PreparedMemory | None = None
```

| 字段 | 类型 | 默认值 | 语义 |
| --- | --- | --- | --- |
| `name` | `str` | 无默认值 | 实际操作名；当前实现取 `"insert_memory"`、`"update_memory"`、`"delete_memory"` |
| `memory_id` | `int \| None` | `None` | 目标父记忆 ID；`insert_memory` 不使用，`update_memory` / `delete_memory` 必填 |
| `prepared` | `PreparedMemory \| None` | `None` | 写操作的计算产物；`delete_memory` 为 `None` |

### `MemoryPlan`

```python
@dataclass
class MemoryPlan:
    operations: list[MemoryOperation] = field(default_factory=list)
    remove_ids: list[str] = field(default_factory=list)
```

| 字段 | 类型 | 默认值 | 语义 |
| --- | --- | --- | --- |
| `operations` | `list[MemoryOperation]` | 空列表 | 本次要提交的记忆写操作，按模型工具调用顺序排列 |
| `remove_ids` | `list[str]` | 空列表 | 需要从图消息队列裁剪的消息 ID，来自 `history_removals` 的安全前缀 |

## 构造或校验

1. **计划构造**：`process_memory_snapshot(payload, memory_store)` 先校验 payload 协议版本、存储开关与新增消息范围，再校验模型全部工具调用参数，最后：
   - `plan = MemoryPlan(remove_ids=[item.id for item in history_removals(messages, trim_index)])`；
   - 对每条写操作调用 `await memory_store.prepare_memory(text, importance)` 得到 `PreparedMemory`（`delete_memory` 为 `None`），并追加 `MemoryOperation(name, memory_id, prepared)`。
2. **产物构造**：`prepare_memory` 内部 `chunk_memory(memory)` 得到 `(chunks, auto_keywords, auto_event_date)`，再用 `get_qwen_embedding_model().encode(text)` 编码每个非空块；`event_date` / `keywords` 为 `None` 时用自动提取值替换；若没有任何非空块则抛 `ValueError("记忆切块为空，不能提交缺少向量块的记录")`。
3. **直接写路径**：`insert_memory` / `update_memory` 自行调用 `prepare_memory` 后构造 `MemoryOperation`，再交给 `_commit_operation`；`delete_memory` 只构造带 `memory_id` 的操作。
4. 三个 dataclass 没有 `__post_init__` 或校验器；合法性由上述生产函数保证，数据库写入时 `apply_operations` 还会检查 `prepared is None`、未知操作名等。

## 生产方

| 生产位置 | 产物 | 触发条件 |
| --- | --- | --- |
| `process_memory_snapshot`（[`agent/memory/processor.py`](../../../../agent/memory/processor.py)） | `MemoryPlan`（含 `MemoryOperation` 与 `PreparedMemory`） | Worker 领取任务并完成检索与总结模型调用，且工具调用全部通过校验 |
| `AsyncPostgresCharacterMemoryStore.prepare_memory`（[`agent/memory/store.py`](../../../../agent/memory/store.py)） | `PreparedMemory` | 被 processor 或直接 CRUD 调用 |
| `insert_memory` / `update_memory` / `delete_memory` / `_commit_operation` | `MemoryOperation`（单条） | 直接写接口调用 |
| 测试 | `MemoryPlan`、`PreparedMemory` 替身 | `tests/test_memory_service.py`、`tests/test_memory_service_postgres.py` |

## 消费方

| 消费位置 | 读取内容 | 用途 |
| --- | --- | --- |
| `MemoryWorker._process`（[`agent/memory/worker.py`](../../../../agent/memory/worker.py)） | `plan.operations`、`plan.remove_ids` 的长度 | 日志统计；把 `plan` 交给 `jobs.finish` |
| `MemoryJobRepository.finish`（[`agent/memory/jobs.py`](../../../../agent/memory/jobs.py)） | `plan.operations`、`plan.remove_ids` | 在同一事务中调用 `store.apply_operations(conn, plan.operations)`，并把 `remove_ids` 与消息指纹写入 `memory_service.results.trim` |
| `AsyncPostgresCharacterMemoryStore.apply_operations` | `operation.name`、`operation.memory_id`、`operation.prepared.text/importance/event_date/keywords/chunks` | 执行 DELETE / INSERT / UPDATE 父表与子表；`update_memory` 先删旧块再写新块 |
| 图节点 `apply_memory_results`（间接） | 结果表中的 `remove_ids`、`fingerprints` | 通过 `result_removals` / `history_removals` 安全裁剪消息 |
| `store.log_changes` | `apply_operations` 收集的 `changes` | 只在提交后记录父记忆完整内容，不含向量块 |

## 输入输出示例

一次“新增一条记忆 + 裁剪 6 条旧消息”的计划：

```python
PreparedMemory(
    text="2026年11月20日，两人约定周六去公园。",
    importance=60,
    event_date="2026-11-20",
    keywords="约定, 公园, 周六",
    chunks=[("2026年11月20日，两人约定周六去公园。", [0.01, 0.02, "..."]), ("周六去公园。", [0.03, "..."] )],
)

MemoryPlan(
    operations=[MemoryOperation(name="insert_memory", memory_id=None, prepared=<PreparedMemory>)],
    remove_ids=["m1", "m2", "m3", "m4", "m5", "m6"],
)
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)
- 兄弟协议：[`../memory/README.md`](../memory/README.md)、[`../state/README.md`](../state/README.md)
- 协作模块：[`../../memory/README.md`](../../memory/README.md)、[`../../memory/processor/README.md`](../../memory/processor/README.md)、[`../../memory/store/README.md`](../../memory/store/README.md)、[`../../memory/jobs/README.md`](../../memory/jobs/README.md)、[`../../memory/worker/README.md`](../../memory/worker/README.md)
- 实现依据：[`agent/memory/processor.py`](../../../../agent/memory/processor.py)、[`agent/memory/store.py`](../../../../agent/memory/store.py)、[`agent/memory/jobs.py`](../../../../agent/memory/jobs.py)、[`agent/memory/worker.py`](../../../../agent/memory/worker.py)
- 测试覆盖（静态阅读交叉核对，未在本页重新执行）：`tests/test_memory_service.py`（`MemoryPlan` 结果裁剪）、`tests/test_memory_service_postgres.py`（`MemoryOperation` / `PreparedMemory` 提交与失败回滚）
- 未验证项：`chunks` 中向量维度由模型决定，当前子表声明为 `vector(1024)`。
