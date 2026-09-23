# memory — 记忆快照、指纹与安全裁剪

## 职责与入口

- 所属类别：`agent/utils/` 支撑模块，**不是图节点**，无图注册名、无路由。
- 源码：[`agent/utils/memory.py`](../../../../agent/utils/memory.py)（模块 docstring：消息快照、增量处理边界和安全裁剪；不访问模型或数据库）。
- 职责：为后台记忆任务提供纯函数：把图状态中的消息冻结成可跨进程传递的 JSON 快照、计算稳定指纹、组装带 `job_key` 的任务载荷，以及把“删除前 N 条消息”的请求转换成保证工具组完整、至少保留 6 条消息的 `RemoveMessage` 列表。
- 公开入口：`freeze_messages`、`message_fingerprint`、`build_memory_payload`、`history_removals`、`result_removals`。
- 调用方式：同步函数，由图内记忆节点、后台处理器与任务仓储在各自阶段直接调用。

## 调用链总览

```text
图内（agent/node/memory.py）
  prepare_memory（create_prepare_memory_node）
    └─ R3 build_memory_payload(state, character_name, thread_id)
         ├─ R1 freeze_messages(state["messages"])
         ├─ R2 message_fingerprint(messages[boundary])   ← 校验 memory_processed_fingerprint
         └─ sha256(规范化 JSON) → payload["job_key"]
  apply_memory_results（create_apply_memory_results_node）
    ├─ R1 freeze_messages(current)                       ← 把当前图消息转成可按 ID 查找的快照
    ├─ R2 message_fingerprint(...)                       ← 校验结果指纹与 through 边界
    └─ R5 result_removals(current, result)               ← 只删除匹配快照的连续前缀
         └─ R4 history_removals(messages, len(prefix))   ← 工具组安全 + 至少保留 6 条

后台（独立 Worker / 任务仓储）
  agent/memory/processor.process_memory_snapshot ── R4 history_removals(messages, trim_index)
  agent/memory/jobs.finish ── R2 message_fingerprint（生成 results 的逐条指纹表）
```

## 构建链

无工厂、无缓存、无依赖注入；所有函数只依赖 `hashlib`、`json` 与 LangChain 消息类型。

## 运行链

### R1. `freeze_messages`

- 定位与签名：`freeze_messages(messages: list) -> list[dict]`，[`agent/utils/memory.py:9`](../../../../agent/utils/memory.py)，同步函数。
- 调用方与条件：
  - R3 内部无条件调用（快照起点）；
  - `apply_memory_results` 在处理每个后台结果时调用，用于把图消息转成可按 ID 检索的快照（[`agent/node/memory.py:112`](../../../../agent/node/memory.py)）；
  - R5 `result_removals` 内部调用。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `messages` | `list`（LangChain 消息对象） | 必填 | 通常为 `AgentState["messages"]` 或当前存活消息列表 |

隐式输入：无（不读取 state 字段）。

功能与内部调用：

1. `messages_to_dict(messages)` 把消息对象转成 dict；
2. 经 `json.loads(json.dumps(..., ensure_ascii=False))` 做一次 JSON 往返，隔离嵌套的 `tool_calls` / `content`，使任务不持有图状态的对象引用；
3. 提取 `ids = [item["data"].get("id") for item in result]`；
4. 校验 `all(ids)`（每条都有非空 ID）且 `len(set(ids)) == len(ids)`（无重复）。

输出：与输入顺序一致的 `list[dict]`，每个元素形如 `{"type": "...", "data": {"id": "...", "content": ..., ...}}`。

副作用：无。

异常与边界：任一消息缺少 ID、ID 为空串或存在重复 ID 时抛 `ValueError("记忆快照要求每条消息有唯一且稳定的 ID")`。空列表合法，返回 `[]`。`ensure_ascii=False` 保留中文原文。

后续去向：返回 R3 作为 `payload["messages"]`；返回 `apply_memory_results` 用于指纹比对；返回 R5。

### R2. `message_fingerprint`

- 定位与签名：`message_fingerprint(message: dict) -> str`，[`agent/utils/memory.py:18`](../../../../agent/utils/memory.py)，同步函数。
- 调用方与条件：
  - R3 校验 `memory_processed_fingerprint` 时调用；
  - `apply_memory_results` 校验后台结果指纹、`through` 边界时多次调用（[`agent/node/memory.py:116-120`](../../../../agent/node/memory.py)）；
  - `agent/memory/jobs.py` 的 `finish` 为结果表生成逐条指纹表（[`agent/memory/jobs.py:119`](../../../../agent/memory/jobs.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `message` | `dict` | 必填 | `freeze_messages` 风格的 `{"type", "data"}` 快照元素 |

隐式输入：哈希字段集合固定为 `("id", "content", "name", "tool_calls", "tool_call_id")`。

功能与内部调用：

1. 取 `data = message["data"]`；
2. 仅抽取上述 5 个字段构造 `relevant`（其他字段如 `additional_kwargs`、`usage_metadata` 不参与指纹）；
3. 对 `{"type": message["type"], "data": relevant}` 做 `json.dumps(ensure_ascii=False, sort_keys=True, separators=(",", ":"))`；
4. 返回 `hashlib.sha256(...).hexdigest()`。

输出：64 位十六进制字符串。

副作用：无。

异常与边界：`message` 缺少 `"data"` 或 `"type"` 键时抛 `KeyError`；字段缺失按 `None` 参与序列化。相同 `type` 与 5 个字段的消息指纹相同；字段以外的元数据变化不会改变指纹。

后续去向：作为 `payload["messages"]` 的逐条指纹、结果表的 `through_fingerprint` 与 `fingerprints` 映射，供 R3/R5 与 `apply_memory_results` 比对。

### R3. `build_memory_payload`

- 定位与签名：`build_memory_payload(state: dict, character_name: str, thread_id: str) -> dict | None`，[`agent/utils/memory.py:28`](../../../../agent/utils/memory.py)，同步函数。
- 调用方与条件：仅 `agent/node/memory.py` 的 `create_prepare_memory_node` 内部调用（[`agent/node/memory.py:32`](../../../../agent/node/memory.py)）；节点在 `memory_storage_enabled` 为真且没有待投递/进行中任务时执行，并捕获 `(ValueError, TypeError, KeyError)` 转为 `memory_warning`。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `state["messages"]` | `list` | `AgentState` | 缺失按 `[]`；必须每条有唯一 ID | 冻结为快照；空列表时返回 `None` |
| `state["memory_processed_through"]` | `str` | `AgentState` | 缺失或空表示从头处理 | 增量边界：只整理该消息之后的新增消息 |
| `state["memory_processed_fingerprint"]` | `str` | `AgentState` | 缺失/空则跳过指纹校验 | 校验边界消息未被改写 |
| `state["turn_id"]` | `str` | `AgentState` | 无默认；缺失抛 `KeyError` | 写入载荷 |
| `state["iteration"]` | `int` | `AgentState` | `state.get("iteration", 0)` | 写入载荷，入队成功后用于扣减 |
| `state["world_state"]` | `dict` | `AgentState` | `state.get("world_state", {})` | 作为后台处理的背景状态 |
| `state["memory_policy_version"]` | `int` | `AgentState` | 仅当键存在时写入 | 标记策略版本，Worker 提交前核对 |
| `state["memory_retrieval_enabled"]` / `memory_storage_enabled` | `bool` | `AgentState` | 仅在存在 `memory_policy_version` 时写入，默认 `True` | 控制后台检索与写库行为 |
| `character_name` | `str` | 节点工厂闭包 | 必填 | 角色表名/任务归属 |
| `thread_id` | `str` | `config.configurable.thread_id` | 必填，节点侧缺失抛 `ValueError` | 会话隔离与结果回执定位 |

隐式输入：无环境变量与数据库；`json`、`hashlib` 与 R1/R2。

功能与内部调用：

1. 调 R1 `freeze_messages(state.get("messages", []))`；结果为空列表时直接返回 `None`。
2. 取 `ids` 与 `processed = state.get("memory_processed_through")`，`start = 0`。
3. 若 `processed` 非空：
   - `processed not in ids` → 抛 `ValueError("已处理消息边界缺失；不能自动重置记忆进度")`；
   - `boundary = ids.index(processed)`；
   - `expected = state.get("memory_processed_fingerprint")`；若 `expected` 非空且 R2 指纹不等于 `expected` → 抛 `ValueError("已处理消息发生变化；不能沿旧进度继续整理")`；
   - `start = boundary + 1`。
4. `start == len(messages)` 表示没有新增消息，返回 `None`。
5. 组装载荷：`version=1`、`character_name`、`thread_id`、`turn_id`、`iteration`、`world_state`、`messages`、`new_message_start=start`、`through_message_id=ids[-1]`、`base_processed_through=processed`；当 state 含 `memory_policy_version` 时追加三个策略字段。
6. 再次 JSON 往返确保可序列化；对不含 `job_key` 的载荷计算 `sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))`，写入 `payload["job_key"]` 并返回。

输出：任务载荷 dict；无消息或没有新增消息时为 `None`。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `version` | `int` | 总是 | 协议版本，当前固定 `1` | `processor.process_memory_snapshot` 校验 |
| `messages` | `list[dict]` | 总是 | 冻结后的完整消息窗口（含已处理部分作背景） | 后台模型上下文与指纹生成 |
| `new_message_start` | `int` | 总是 | 本次新增范围的起始下标（`boundary+1` 或 0） | 处理器限制只整理新增范围 |
| `through_message_id` | `str` | 总是 | 快照最后一条消息 ID | 入队回执与结果回执 |
| `base_processed_through` | `str | None` | 总是 | 进入本次快照前的处理边界 | 审计与幂等判断 |
| `job_key` | `str` | 总是 | 载荷内容哈希，相同载荷得到相同 key | `jobs.enqueue` 去重与结果表回执 |
| `memory_policy_version` 等 | `int` / `bool` | 仅 state 含 `memory_policy_version` | 策略快照 | `memory.policy.memory_permitted` 校验 |

副作用：无（不写库、不入队；入队由后续节点完成）。

异常与边界：三类异常——ID 缺失/重复（R1）、边界缺失、边界被改写；调用方 `create_prepare_memory_node` 捕获后返回 `memory_warning`，不抛到图外。`turn_id` 缺失抛 `KeyError` 同样被节点捕获。

后续去向：`prepare_memory` 节点把它写入 `memory_pending_job`；下一节点 `enqueue_memory` 交给 `agent/memory/jobs.enqueue` 入队。

### R4. `history_removals`

- 定位与签名：`history_removals(messages: list, index: int | None) -> list[RemoveMessage]`，[`agent/utils/memory.py:63`](../../../../agent/utils/memory.py)，同步函数。
- 调用方与条件：
  - `agent/memory/processor.process_memory_snapshot` 把模型给出的 `clean_history.index` 转成 `MemoryPlan.remove_ids`（[`agent/memory/processor.py:235`](../../../../agent/memory/processor.py)）；
  - R5 `result_removals` 内部复用同一安全规则。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `messages` | `list` | 必填 | 快照消息列表（LangChain 消息对象） |
| `index` | `int | None` | 必填 | 希望从开头删除的消息条数；来自模型工具参数或 R5 的前缀长度 |

隐式输入：无（不读 state、不读配置）。

功能与内部调用：

1. `type(index) is not int or index <= 0` → 返回 `[]`（`bool` 也不接受，因为 `type(True) is not int`）。
2. `limit = min(index, max(0, len(messages) - 6))`：裁剪点最多推进到距末尾 6 条处，保证删除后至少保留 6 条消息。
3. 遍历 `messages[:limit]`，用 `pending` 集合跟踪未闭合的工具调用：
   - `AIMessage` → 把其全部 `tool_calls` 的 `id` 加入 `pending`；
   - `ToolMessage` → 从 `pending` 移除对应 `tool_call_id`；
   - 每当 `pending` 为空，把 `safe_index` 更新为当前位置 + 1。
4. 返回 `messages[:safe_index]` 中所有非空 `id` 对应的 `RemoveMessage`。

输出：`list[RemoveMessage]`；不满足条件时为空列表。`safe_index` 只落在完整工具组之后，因此不会把 `AIMessage` 工具请求与其 `ToolMessage` 结果拆开。

副作用：无（不修改入参消息）。

异常与边界：`messages` 元素没有 `tool_calls`/`tool_call_id`/`id` 时按属性访问实际行为处理（非 AI/Tool 消息不参与 pending 逻辑）；消息缺少 `id` 时该条不会被删除（过滤 `getattr(message, "id", None)`）。上限检查在 `limit` 处完成，因此即使 `index` 极大也不会删到最近 6 条以内。

后续去向：处理器写入 `MemoryPlan.remove_ids`，随结果回执返回图内；R5 返回给 `apply_memory_results` 作为 `AgentState["messages"]` 的增量。

### R5. `result_removals`

- 定位与签名：`result_removals(messages: list, result: dict) -> list[RemoveMessage]`，[`agent/utils/memory.py:81`](../../../../agent/utils/memory.py)，同步函数。
- 调用方与条件：`agent/node/memory.py` 的 `apply_memory_results`，在后台结果通过 `through` 边界与指纹校验后调用（[`agent/node/memory.py:121`](../../../../agent/node/memory.py)）。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `messages` | `list` | `AgentState["messages"]` 当前值 | 必填；每条须有唯一 ID | 与结果快照比对 |
| `result["remove_ids"]` | `list[str]` | `memory_service.results.trim` | 缺失按 `[]` | 后台建议删除的消息 ID |
| `result["fingerprints"]` | `dict[str, str]` | `memory_service.results.trim` | 缺失按 `{}` | 逐条指纹校验表 |

隐式输入：R1 `freeze_messages`、R2 `message_fingerprint`、R4 `history_removals`。

功能与内部调用：

1. 读取 `requested`、`fingerprints`；调 R1 冻结当前消息，得到 `ids` 与 `existing` 集合。
2. `remaining = [id for id in requested if id in existing]`（已不在图中的 ID 被忽略）。
3. 前缀校验：`ids[:len(remaining)] != remaining` → 返回 `[]`。即剩余 ID 必须仍是当前消息列表的连续前缀且顺序一致，否则整体放弃裁剪。
4. 指纹校验：对当前前缀逐条比较 `fingerprints.get(id)` 与 R2 计算值，任一不匹配 → 返回 `[]`。
5. 通过后调用 R4 `history_removals(messages, len(remaining))`，把“前缀长度”再交给工具组安全与 6 条下限规则。

输出：可安全删除的 `RemoveMessage` 列表；快照不匹配或边界不安全时为 `[]`（调用方保留原文并继续同步后续结果）。

副作用：无。

异常与边界：R1 在消息 ID 缺失/重复时抛 `ValueError`，由 `apply_memory_results` 的外层 `try` 捕获并转为 `memory_warning`。`fingerprints` 缺少某个前缀 ID 的条目时视为不匹配（`.get` 返回 `None`），裁剪被放弃。

后续去向：`apply_memory_results` 把返回值作为 `updates["messages"]`，并更新 `memory_processed_through` / `memory_processed_fingerprint` / `memory_last_applied_job`。

## 分支与异常链

| 条件 | 行为 | 终点 |
| --- | --- | --- |
| 快照无消息 / 无新增消息 | R3 返回 `None` | `prepare_memory` 返回 `{}`，不入队 |
| 已处理边界 ID 不在消息中 | R3 抛 `ValueError` | 节点捕获，写 `memory_warning`，保留原文与进度 |
| 边界消息指纹变化 | R3 抛 `ValueError` | 同上 |
| `memory_processed_through` 缺失 | `start=0`，整窗作为新增处理 | 正常载荷 |
| `history_removals` 的 `index` 非正整数 | 返回 `[]` | 不裁剪 |
| `index` 超过 `len(messages)-6` | 裁剪点被限制在距末尾 6 条 | 至少保留 6 条 |
| 工具组未闭合 | `safe_index` 停在组前 | 请求与结果一起保留 |
| 结果前缀不连续/顺序不符 | R5 返回 `[]` | 跳过裁剪，仍确认结果 |
| 结果前缀指纹不匹配 | R5 返回 `[]` | 跳过裁剪，仍确认结果 |
| 消息 ID 缺失/重复 | R1 抛 `ValueError` | 节点捕获，写 `memory_warning` |

## 输入输出示例

适用 R3（示意，字段与实现一致，角色与内容为虚构占位）：

```json
{
  "version": 1,
  "character_name": "SuLi",
  "thread_id": "ui:thread-demo",
  "turn_id": "turn-1",
  "iteration": 3,
  "world_state": {"time": {"date": "2026-09-23", "weekday": "星期三", "period": "下午"}, "weather": "少云 32°C"},
  "messages": [
    {"type": "human", "data": {"id": "u1", "content": "<timestamp>...</timestamp>\n你好"}},
    {"type": "ai", "data": {"id": "a1", "content": "你好。"}}
  ],
  "new_message_start": 1,
  "through_message_id": "a1",
  "base_processed_through": "u1",
  "job_key": "<sha256(除 job_key 外的规范化载荷)>"
}
```

适用 R4（与 `tests/test_reply_memory.py:179-196` 的断言结构一致）：

```text
输入: messages = [Human(id="u"), AI(id="ai1", tool_calls=[call1, call2]),
                  Tool(id="tool1"), Tool(id="tool2"), Human(id="u0".."u5")]
history_removals(messages, 1)   → [RemoveMessage(id="u")]
history_removals(messages, 3)   → [RemoveMessage(id="u")]          # 工具组未闭合，不越过 ai1
history_removals(messages, 4)   → [RemoveMessage(id="u"), RemoveMessage(id="ai1"),
                                   RemoveMessage(id="tool1"), RemoveMessage(id="tool2")]
history_removals(messages, 999) → 同上（limit 被限制为 len-6）
history_removals(messages, -1)  → []
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)（agent/utils 总览）
- 调用方文档：[`../../node/memory/README.md`](../../node/memory/README.md)（图内交接）、[`../../memory/processor/README.md`](../../memory/processor/README.md)（后台计划）、[`../../memory/jobs/README.md`](../../memory/jobs/README.md)（结果回执）
- 相关模块：[`../context/README.md`](../context/README.md)（同一状态字段的 token 估算）、[`../../classes/state/README.md`](../../classes/state/README.md)（`AgentState` 记忆字段定义）
- 调用方源码：[`agent/node/memory.py:6`](../../../../agent/node/memory.py)、[`agent/memory/processor.py:9`](../../../../agent/memory/processor.py)、[`agent/memory/jobs.py:8`](../../../../agent/memory/jobs.py)
- 已有测试覆盖（本次未执行）：`tests/test_reply_memory.py:179-196`（工具组完整与 6 条下限、非法 index）、`tests/test_conversation_policy.py:9,34-36`（`build_memory_payload` 策略字段）、`tests/test_memory_service.py`、`tests/test_memory_service_postgres.py`（载荷与指纹）
- 验证情况：本页为静态阅读源码所得；`job_key` 去重与结果表指纹的实际数据库行为见记忆服务专题与 `agent/memory/jobs.py`，本次文档编写未实际执行测试。
