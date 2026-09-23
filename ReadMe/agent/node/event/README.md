# event 节点（事件完成判断路由）

## 职责与入口

- 所属类别：条件路由函数（不是模型节点）。
- 源码：[agent/node/event.py](../../../../agent/node/event.py)
- 图注册名：`event_judge`，作为 `tts` 的条件边（[agent/builder.py](../../../../agent/builder.py)）。
- 触发时机：每轮 `tts` 节点执行后；返回 `True` 进入 `prepare_memory`，`False` 直接 `END`。
- 实现说明：当前版本**不再调用语义事件判断模型**，改为按轮数上下限与上下文 token 压力判断；`get_event_judge_prompt` 仍注册但未接入主链（见 [../../prompts/tools/event_judge/README.md](../../prompts/tools/event_judge/README.md)）。

## 调用链总览

| 步骤 | 函数 | 关系 |
| --- | --- | --- |
| R1 | `event_judge(state) -> bool` | 图条件边 |
| R1.1 | `_context_tokens(messages)` | 内部函数调用 |
| R1.2 | `estimate_message_tokens(message)` | 无 usage 时的估算回退（[../../utils/context/README.md](../../utils/context/README.md)） |

## 运行链

### R1. `event_judge`

- 定位与签名：`event_judge(state: AgentState) -> bool`，同步路由，[agent/node/event.py:33](../../../../agent/node/event.py#L33)。
- 调用方与条件：LangGraph 在 `tts` 之后调度。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `memory_storage_enabled` | `bool` | 服务端每轮输入 | 缺省 `True` | 关闭时不进入记忆处理 |
| `memory_pending_job` | `dict` 或 `None` | 上一轮交接 | 缺省 `None` | 非空表示还有未投递快照，避免重叠任务 |
| `memory_active_job` | `int` 或 `None` | 上一轮交接 | 缺省 `None` | 非空表示已有未同步任务，避免重叠任务 |
| `need_event_judge` | `bool` | 初始输入 | 缺省 `False` | 总开关；服务端按 `memory_storage_enabled` 传入 |
| `iteration` | `int` | `update_iter` | 缺省 `0` | 轮数判断 |
| `messages` | `list` | checkpoint | 缺省 `[]` | token 用量判断 |

隐式输入：`config.config.MINIMUM_ITERATIONS`（默认 3）、`MAXIMUM_ITERATIONS`（默认 20）、`MEMORY_TOKEN_THRESHOLD`（默认 20000）。

功能与内部调用：

1. `memory_storage_enabled` 为假 → `False`。
2. `memory_pending_job` 或 `memory_active_job` 非空 → `False`（队列中已有任务时不再生成新任务，后续消息继续积累）。
3. `need_event_judge` 为真时按轮数判断：
   - `MINIMUM_ITERATIONS <= iteration <= MAXIMUM_ITERATIONS`：调用 `_context_tokens(messages)`（R1.1）；达到 `MEMORY_TOKEN_THRESHOLD` 返回 `True`，否则 `False`；
   - `iteration > MAXIMUM_ITERATIONS`：返回 `True`（强制处理）；
   - `iteration < MINIMUM_ITERATIONS`：返回 `False`。
4. 其他情况返回 `False`。

### R1.1. `_context_tokens`

- 定位与签名：`_context_tokens(messages: list) -> int`，同步私有函数，[agent/node/event.py:18](../../../../agent/node/event.py#L18)。
- 行为：反向查找第一条带 `usage_metadata` 的 `AIMessage`；字典用 `input_tokens` 或 `total_tokens`，对象用同名属性；取到即返回。
- 回退：没有任何 AI 消息带用量信息时，对全部消息调用 `estimate_message_tokens`（R1.2）求和（UTF-8 字节近似）。

| 输出 | 类型 | 产生条件 | 含义 | 消费方 |
| --- | --- | --- | --- | --- |
| 布尔路由值 | `bool` | 总是 | `True` → `prepare_memory`；`False` → `END` | LangGraph 条件边 |

副作用：仅在达到阈值时写一条 info 日志。异常与边界：`usage_metadata` 字段缺失或为 0 时继续向前查找；配置值非法时由 `config.config` 的 `int()` 在导入期抛错。

后续去向：

| 条件 | 返回值 | 对应下一节点 | 结束或回接位置 |
| --- | --- | --- | --- |
| 存储开启、无挂起任务，且（`iteration > MAXIMUM_ITERATIONS` 或区间内 token 达阈值） | `True` | `prepare_memory` | `prepare_memory → enqueue_memory → END` |
| 存储关闭 / 已有任务 / 轮数不足 / token 未达阈值 | `False` | `END` | 本轮结束 |

## 分支与异常链

- **存储关闭**：永远 `False`，不产生记忆任务。
- **已有任务**：`pending`/`active` 非空时跳过；结果应用与补投由 `apply_memory_results` 在轮初处理（见 [../memory/README.md](../memory/README.md)）。
- **轮数超过上限**：即使 token 未达阈值也强制进入记忆处理，避免上下文无限增长。
- **token 估算回退**：模型未返回用量时按消息字节数估算，不调用额外模型。

## 输入输出示例

适用 R1（区间内达到阈值）：

```text
输入：memory_storage_enabled=True，need_event_judge=True，iteration=7，
      memory_pending_job=None，memory_active_job=None，
      messages[-1]=AIMessage(usage_metadata={"input_tokens": 21500})
输出：True → prepare_memory
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 下游：[../memory/README.md](../memory/README.md)
- token 估算：[../../utils/context/README.md](../../utils/context/README.md) · 配置：[../../../config/README.md](../../../config/README.md)
- 依据：`agent/node/event.py`；`tests/test_event_judge.py` 覆盖轮数/token 路由；本次未执行测试。
