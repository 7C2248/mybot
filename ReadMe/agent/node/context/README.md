# context 节点（模型上下文限制）

## 职责与入口

- 所属类别：图节点（无工厂，普通同步函数）。
- 源码：[agent/node/context.py](../../../../agent/node/context.py)
- 图注册名：`limit_context` → `limit_context`（[agent/builder.py](../../../../agent/builder.py)）。
- 上游/下游：`apply_memory_results` 固定边到 `limit_context`，再由固定边到 `world_state_update`。
- 触发时机：每轮在记忆结果应用后、世界状态更新前执行一次；只裁剪模型可见上下文，不删除 UI 正式历史。

## 调用链总览

| 步骤 | 函数 | 关系 |
| --- | --- | --- |
| R1 | `limit_context(state)` | 图调度 |
| R1.1 | `estimate_message_tokens(message)` | 直接函数调用（[../../utils/context/README.md](../../utils/context/README.md)） |

## 运行链

### R1. `limit_context`

- 定位与签名：`limit_context(state)`，同步函数，[agent/node/context.py:8](../../../../agent/node/context.py#L8)。
- 调用方与条件：LangGraph 固定边调度。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `memory_policy_version` | `int` | 服务端每轮初始输入 | 缺失时函数直接返回 `{}` | 受管会话标记；CLI 旧路径不裁剪 |
| `messages` | `list` | checkpoint | 缺省 `[]` | 待裁剪的模型上下文 |
| `memory_retrieval_enabled` | `bool` | 服务端每轮初始输入 | 缺省 `False` | 关闭检索时隐藏历史记忆工具消息 |

隐式输入：`config.config.MODEL_CONTEXT_TOKEN_BUDGET`（在函数内导入；环境变量，默认 16000，最小 2048）。

功能与内部调用：

1. **受管会话门槛**：`'memory_policy_version' not in state` 时返回 `{}`，不做任何裁剪。
2. **隐藏记忆工具组**：`memory_retrieval_enabled` 为假时，遍历消息：
   - `AIMessage` 且 `tool_calls` 中含 `name == "memory_query"` → 删除该 AI 消息，并收集其工具调用 ID；
   - `ToolMessage` 且 `tool_call_id` 属于被删调用或 `name == "memory_query"` → 删除该工具结果。
   目的是关闭检索后不把旧的记忆工具调用/结果继续暴露给模型。
3. **按用户轮次裁剪**：在剩余消息上：
   - `starts` 为所有 `HumanMessage` 的下标；
   - `budget` 为剩余消息的估算 token 总和（R1.1）；
   - 从第二个用户轮次开始尝试切点：当 `budget <= MODEL_CONTEXT_TOKEN_BUDGET` 且切点后消息数 `<= 120` 时停止；否则扣除 `[cut, start)` 区间消息的 token 数并把 `cut` 移到该用户消息处。
   - 只从用户消息处切分，保证工具调用与工具结果不会被拆散；最新一轮始终保留（`starts[1:]`）。
4. 把 `remaining[:cut]` 的 ID 加入删除集合，返回对应的 `RemoveMessage` 列表；没有可删消息时返回 `{}`。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `messages` | `list[RemoveMessage]` | 存在待隐藏或待裁剪消息 | `add_messages` 按 ID 显式删除，不截断列表 | 后续节点、checkpoint |

副作用：无数据库或模型调用；只产生状态增量。`RemoveMessage` 会写入 checkpoint，因此后续轮次看到的上下文更短。

异常与边界：

- 单条消息 token 估算使用 UTF-8 字节近似（ASCII 约 4 字节/token，非 ASCII 约 2 字节/token），不是真实分词。
- 只有 `HumanMessage` 位置可作为切点；若上下文没有第二个用户轮次则完全不裁剪（即使超预算）。
- 关闭检索时先隐藏记忆工具组再计算预算；隐藏的消息不会被重新加入。

后续去向：返回增量由框架合并，固定边到 `world_state_update`。

## 分支与异常链

- **非受管会话（无 `memory_policy_version`）**：直接返回 `{}`，不隐藏工具消息、不裁剪。
- **检索开启**：不隐藏记忆工具消息，只按预算裁剪。
- **预算足够且消息数 ≤ 120**：不做任何裁剪。
- **消息无 ID**：`RemoveMessage(id=m.id)` 在 `m.id` 为 `None` 时由框架报错；正常情况下 checkpoint 中的消息都有稳定 ID。

## 输入输出示例

适用 R1，检索关闭且存在旧记忆工具组：

```text
输入：messages = [AIMessage(id="a1", tool_calls=[{name:"memory_query", id:"c1"}]),
                  ToolMessage(id="t1", tool_call_id="c1", name:"memory_query"),
                  HumanMessage(id="u1"), AIMessage(id="r1"), HumanMessage(id="u2"), ...]
输出：{"messages": [RemoveMessage(id="a1"), RemoveMessage(id="t1"), ...裁剪前缀消息]}
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 下游：[../world_state/README.md](../world_state/README.md)
- 工具：[../../tools/memory_query/README.md](../../tools/memory_query/README.md) · token 估算：[../../utils/context/README.md](../../utils/context/README.md)
- 配置：[../../../config/README.md](../../../config/README.md)
- 依据：`agent/node/context.py`；`tests/test_rp_pipeline.py`、`tests/test_reply_memory.py` 覆盖上下文裁剪相关行为；本次未执行测试。
