# AgentState（全图共享状态协议）

## 职责与入口

- 所属类别：状态协议（`agent/classes/state.py`），不是图节点，没有工厂函数、路由函数或可执行入口。
- 源码：[`state.py`](../../../../agent/classes/state.py)
- 注册位置：[`agent/builder.py`](../../../../agent/builder.py) 中 `workflow = StateGraph(AgentState)`；checkpointer 按 channel 持久化整份状态。
- 使用方：全部图节点通过参数 `state` 读取并返回状态增量；`server/services/agent.py` 负责注入初始输入、投影公开状态与检查点修复；`server/services/checkpoint_sync.py` 读取记忆标记；测试直接以 dict 调用节点或图。

## 定义

`class AgentState(TypedDict, total=False)` 只做静态形状声明：`total=False` 表示所有键都可缺失，类型注解不参与运行时校验。`messages` 使用 LangGraph 的 `add_messages` reducer，其余键由框架按“后写覆盖”合并。字段分三组：

### 1. 对话与外部输入字段

| 字段 | 类型 | 写入方（生产） | 读取方（消费） | 缺省与含义 |
| --- | --- | --- | --- | --- |
| `messages` | `Annotated[list, add_messages]` | 调用方输入 `HumanMessage`（服务端 `_input` 生成 id=`user_{user_message_id}`、正文含 `<timestamp>`）；draft 返回模型 `AIMessage`（含工具调用）；LangGraph `ToolNode` 返回 `ToolMessage`；`commit_reply` 返回 id=`reply_{turn_id}` 的 `AIMessage`；`begin_turn`、`reply_failed`、`limit_context`、`apply_memory_results` 返回 `RemoveMessage` | 全部节点；`build_memory_payload` 快照；checkpointer 持久化 | `state.get("messages", [])`。`add_messages` 按 ID 追加或替换，`RemoveMessage` 显式删除；返回切片不会截断队列 |
| `iteration` | `int` | `increment_iteration` 每次成功提交 `+1`；`_enqueue_pending` 入队成功后扣减 `max(0, iteration - payload["iteration"])`；`server/services/agent.py` 修复与策略版本切换时重置 0 | `event_judge` 与 `MINIMUM_ITERATIONS`/`MAXIMUM_ITERATIONS` 比较；`build_memory_payload` 记录任务轮次 | `state.get("iteration", 0)`；用于控制记忆处理时机 |
| `need_event_judge` | `bool` | 服务端 `execute` 输入 `run['memory_storage_enabled']`；修复流程重置字典默认置 `True`（随后可能被恢复状态覆盖） | `event_judge` 的事件判断开关 | `state.get("need_event_judge", False)`；外部参数，缺省视为关闭 |
| `need_tts` | `bool` | 服务端 `execute` 输入 `False`；`server/services/speech.py` 单独调用 tts 节点时传 `True` | `create_tts_node` 内部 `tts_node` | `state.get("need_tts", False)`；关闭时 tts 节点直接返回 `{}` |
| `world_state` | `dict` | `update_world_state` 返回 `{"time": {...}, "weather": ...}`（时间取系统时间，天气取和风天气）；修复时置 `{}` | `prepare_world_state`（draft、participant_state、`processor`）；`server/services/agent.py` 的 `public_state` | `state.get("world_state")`；`prepare_world_state(None)` 会生成 time 且 weather 为 `None` |
| `character_state` | `dict` | participant_state 节点合并写回；导入/修复时置 `{}` | `prepare_character_state`（draft、participant_state）；`public_state` 投影 | `state.get("character_state")`；键为 `location`、`mood`、`body`、`clothing`、`hearing` |
| `user_state` | `dict` | participant_state 节点合并写回；导入/修复时置 `{}` | `prepare_user_state`（draft、participant_state）；`public_state` 投影 | `state.get("user_state")`；键为 `location`、`mood`、`body`、`clothing`（无 `hearing`） |

### 2. 轮内候选回复与检查字段（`begin_turn` 轮初重置）

| 字段 | 类型 | 写入方（生产） | 读取方（消费） | 缺省与含义 |
| --- | --- | --- | --- | --- |
| `turn_id` | `str` | `begin_turn` 取 `service_run_id`，否则 `uuid.uuid4().hex`；修复清空 | `commit_reply` 校验并生成消息 ID；`check`/`event`/`draft` 日志；`build_memory_payload` 必填 | 无声明默认值；缺失时 `build_memory_payload` 抛 `KeyError`，`prepare_memory` 捕获后写 `memory_warning` |
| `service_run_id` | `str` | 服务端 `execute` 输入为 run ID 字符串；修复清空 | `begin_turn` 用作 `turn_id`；`increment_iteration` 决定是否置 `service_reply_counted`；修复判断状态归属 | 服务端 UI 运行使用，CLI 侧为空 |
| `service_reply_counted` | `bool` | `begin_turn` 置 `False`；`increment_iteration` 在存在 `service_run_id` 时置 `True` | 服务端修复读取，避免恢复时重复增加轮数 | 无声明默认值；`state.get(...)` 为假值时视为未计数 |
| `draft_reply` | `str` | draft 成功时写正文；`begin_turn`、`reply_failed`、`commit_reply` 清空 | check 节点检查；`commit_reply` 校验；draft 修订时作为 `<previous_draft>` | `state.get("draft_reply", "")`；空串表示无可提交候选 |
| `draft_reasoning` | `str` | draft 写 `additional_kwargs.reasoning_content`；`begin_turn`、`reply_failed`、`commit_reply` 清空 | `check._rule_issues` 的拒答正则检测；`commit_reply` 写入消息 `additional_kwargs` | `state.get("draft_reasoning") or ""` |
| `draft_status` | `str` | draft 写 `calling_tools`/`ready`/`failed`；`begin_turn` 写 `pending`；`commit_reply` 写 `committed`；`reply_failed` 写 `failed`；修复写 `committed` 或 `pending` | `draft_judge` 路由；check 要求 `ready`；`commit_reply` 要求 `ready` | 无声明默认值；取值集合即上述六种 |
| `draft_usage` | `dict \| None` | draft 将模型 `usage_metadata` 转为纯 dict；`begin_turn`、`reply_failed`、`commit_reply` 清空 | `commit_reply` 作为正式 `AIMessage` 的 `usage_metadata` | `state.get("draft_usage")`；`None` 表示模型未返回用量 |
| `check_feedback` | `str` | check 失败时拼接问题清单；`begin_turn`、`reply_failed`、`commit_reply` 清空 | draft 拼入 system prompt 触发修订 | `state.get("check_feedback") or ""`；空串表示无修订意见 |
| `check_rounds` | `int` | check 每次执行 `+1`；`begin_turn` 置 0；修复置 0 | `check_judge` 与 `_MAX_CHECK_ROUNDS`（5）比较决定回 draft 还是 reply_failed | `state.get("check_rounds", 0)` |
| `check_status` | `str` | check 写 `passed`/`failed`/`unavailable`；`begin_turn` 写 `pending`；修复写 `pending` | `check_judge` 路由；`commit_reply` 要求 `passed` | 无声明默认值；`unavailable` 表示接口重试耗尽、不得放行 |
| `check_issues` | `list[dict]` | check 失败时写 `CheckIssue.model_dump()` 列表，通过时写 `[]`；`begin_turn` 置 `[]` | draft 判断是否存在 `refusal` 类型；`commit_reply` 要求为空 | `state.get("check_issues") or []`；元素结构见 [`../check/README.md`](../check/README.md) |
| `check_reply` | `str` | check 通过时写实际通过检查的文本；失败/不可用与 `begin_turn`、`reply_failed`、`commit_reply` 清空 | `commit_reply` 要求与 `draft_reply` 完全一致，防止检查后变化 | `state.get("check_reply")`；空串表示未通过 |
| `reply_error` | `str` | `reply_failed` 写失败文案；`begin_turn`、修复清空 | 服务端 `execute` 结束后据此判定 `error` 并触发修复 | `state.get("reply_error")`；图路由不读它 |
| `retry_message_id` | `str` | `reply_failed` 写最近一条 `HumanMessage` 的 ID；初始与 `begin_turn`、`commit_reply`、修复清空 | `begin_turn` 用于合并失败输入之后的重复提交（仅紧随其后的相同输入） | `state.get("retry_message_id")`；空串表示无需去重 |

### 3. 后台记忆交接字段（轮初不重置，与消息裁剪共同 checkpoint）

| 字段 | 类型 | 写入方（生产） | 读取方（消费） | 缺省与含义 |
| --- | --- | --- | --- | --- |
| `memory_pending_job` | `dict \| None` | `create_prepare_memory_node` 写 `build_memory_payload` 结果；`_enqueue_pending` 成功后置 `None`；服务端关闭存储/切换策略置 `None` | `prepare`/`enqueue`/`event_judge` 防重守卫；`checkpoint_sync` 读取 `through_message_id` | `state.get("memory_pending_job")`；非空表示尚未成功入队的不可变快照 |
| `memory_active_job` | `int \| None` | `_enqueue_pending` 写入 `jobs.enqueue` 返回的 job_id；`apply_results` 在结果 job_id ≥ active 时置 `None` | `event_judge` 守卫；`apply_results` 查询失败状态；`checkpoint_sync` 标记 `active` | `state.get("memory_active_job")`；非空表示已入队未同步结果 |
| `memory_submitted_through` | `str` | `_enqueue_pending` 写 `payload["through_message_id"]` | `checkpoint_sync` 标记 `submitted` | `state.get("memory_submitted_through")`；已成功投递到的消息 ID |
| `memory_processed_through` | `str` | `apply_results` 写 `result["through_message_id"]`；修复与策略切换清空 | `build_memory_payload` 作为下一批新增范围起点；服务端 `memory_source` 传给 `repo.memory_messages`；`checkpoint_sync` | `state.get("memory_processed_through")`；边界必须在当前消息中，否则 `build_memory_payload` 抛 `ValueError` |
| `memory_processed_fingerprint` | `str` | `apply_results` 写 `result["through_fingerprint"]` | `build_memory_payload` 校验边界消息未变化 | `state.get("memory_processed_fingerprint")`；与边界指纹不一致时抛 `ValueError` |
| `memory_trimmed_through` | `str` | `apply_results` 写本次实际删除的最后一条消息 ID；修复与策略切换清空 | 仅持久化记录 | `state.get("memory_trimmed_through")`；已裁剪到的消息 ID |
| `memory_last_applied_job` | `int` | `apply_results` 对每个已确认结果写 `job_id`；策略切换时设为 results 表当前最大 job_id | `apply_results` 起点（`state.get("memory_last_applied_job", 0)`）与 `jobs.acknowledge` | 缺省回退 0；与 `RemoveMessage` 一起保存的结果确认 |
| `memory_warning` | `str` | `prepare`/`_enqueue_pending`/`apply_results` 写降级文案，成功同步时清空 | 服务端 `finish` 将其映射为 `warning="memory_delayed"` | `state.get("memory_warning")`；后台异常不写入 `reply_error` |
| `memory_retrieval_enabled` | `bool` | 服务端 `execute` 输入；修复不重置 | `builder.build_rp_agent` 决定是否注入 `memory_query` 工具；`limit_context` 隐藏工具消息；`build_memory_payload` | `state.get('memory_retrieval_enabled', False)`（`limit_context`）与 `True`（`build_memory_payload`）两种回退 |
| `memory_storage_enabled` | `bool` | 服务端 `execute` 输入；修复不重置 | `prepare`/`enqueue`/`apply_results`/`event_judge` 的守卫；服务端生成 `need_event_judge` | `state.get('memory_storage_enabled', True)`；关闭时各记忆节点返回 `{}` |
| `memory_policy_version` | `int` | 服务端 `execute` 输入（会话策略版本） | `limit_context` 以此判断是否为托管会话；`apply_results` 选择“托管快照”同步路径；`build_memory_payload` 写入 payload；Worker 侧 `memory_permitted` 比对 | `'memory_policy_version' in state` 判断存在性；缺省不存在时跳过托管逻辑 |

## 构造或校验

本协议没有 `__init__`、工厂函数或 `model_validator`：

- 图的调用方直接以 dict 输入（服务端 `AgentAdapter.execute` 的 `inputs`，或测试的 `graph.ainvoke({...})`）；LangGraph 为每个键建立 channel。
- 节点返回的都是状态增量 dict，不是完整 `AgentState`；未返回的键保持原值。
- 运行时没有类型校验，字段约束由使用方自行保证：
  - `messages` 的 ID 稳定性与唯一性由 [`agent/utils/memory.py`](../../../../agent/utils/memory.py) 的 `freeze_messages` 强制（缺 ID 或重复 ID 抛 `ValueError`），供记忆快照使用。
  - `build_memory_payload` 校验 `memory_processed_through` 必须存在于当前消息、且指纹一致，否则抛 `ValueError`。
  - 服务端修复流程通过 `graph.aupdate_state` 注入完整重置字典，并用 `RemoveMessage(id=REMOVE_ALL_MESSAGES)` 重建消息列表。
- 服务端 `freeze_state`/`thaw_state` 负责消息与 JSON 的相互转换；`thaw_state` 使用 `messages_from_dict` 还原消息对象。

## 生产方

| 生产位置 | 写入字段 | 触发条件 |
| --- | --- | --- |
| 服务端 `AgentAdapter.execute` 的 `inputs` | `messages`（新用户消息）、`service_run_id`、`need_tts=False`、`need_event_judge`、`memory_retrieval_enabled`、`memory_storage_enabled`、`memory_policy_version`，策略切换时追加记忆字段重置 | 每次运行图前 |
| `begin_turn`（[`agent/node/state.py`](../../../../agent/node/state.py)） | `turn_id`、`draft_*`、`check_*`、`reply_error`、`retry_message_id`、`service_reply_counted`，必要时 `messages`（删除重复提交） | 图入口 |
| `update_world_state` | `world_state` | 每轮 |
| participant_state 节点 | `character_state`、`user_state` | `participant_state_in` / `participant_state_out`，且消息对有效 |
| draft 节点 | `draft_reply`、`draft_status`、`draft_reasoning`、`draft_usage`、`check_status/check_issues/check_reply` 重置、工具调用消息 | 模型调用成功或重试耗尽 |
| check 节点 | `check_status`、`check_rounds`、`check_issues`、`check_feedback`、`check_reply` | 规则或模型检查出结果 |
| `commit_reply` | 正式 `AIMessage`、`draft_*`、`check_reply`、`reply_error` 清理 | 检查通过且草稿未变化 |
| `reply_failed` | `reply_error`、`retry_message_id`、`draft_*`、`check_*` 清理、`RemoveMessage` | 草稿或检查失败 |
| `increment_iteration` | `iteration`、`service_reply_counted` | 正式提交后 |
| `limit_context` | `messages`（`RemoveMessage`） | 存在 `memory_policy_version` 时 |
| prepare/enqueue/apply 记忆节点 | 全部 `memory_*` 字段 | 见 [`../memory_job/README.md`](../memory_job/README.md) 与 `../node/memory/README.md` |
| 服务端修复 `repair` | 上表第 2、3 组字段的重置值与消息重建 | 失败运行恢复时 |

## 消费方

| 消费位置 | 读取字段 | 用途 |
| --- | --- | --- |
| draft 节点 | `messages`、`world_state`、`character_state`、`user_state`、`check_feedback`、`check_issues`、`draft_reply` | 组装 system prompt 与模型上下文；修订时携带上一版草稿 |
| check 节点与 `check_judge` | `draft_reply`、`draft_status`、`draft_reasoning`、`check_rounds`、`messages`、`turn_id` | 本地规则 + 模型检查；路由到 commit_reply / draft / reply_failed |
| `commit_reply` | `draft_reply`、`draft_status`、`check_status`、`check_issues`、`check_reply`、`turn_id`、`draft_reasoning`、`draft_usage` | 提交稳定 ID 的正式回复 |
| `begin_turn` | `service_run_id`、`retry_message_id`、`reply_error`、`messages` | 轮初重置与重试去重 |
| `event_judge` | `memory_storage_enabled`、`memory_pending_job`、`memory_active_job`、`need_event_judge`、`iteration`、`messages` | 是否进入记忆处理 |
| tts 节点 | `need_tts`、`messages` | 语音生成开关与待朗读文本 |
| `limit_context` | `memory_policy_version`、`memory_retrieval_enabled`、`messages` | 工具消息隐藏与按 token 预算裁剪 |
| `agent/utils/memory.py` | `messages`、`turn_id`、`iteration`、`world_state`、`memory_processed_*`、`memory_*_enabled`、`memory_policy_version` | 构建后台任务快照与结果裁剪 |
| `server/services/agent.py` | `world_state`、`character_state`、`user_state`（`public_state`）；`service_reply_counted`、`service_run_id`、`memory_*`（修复） | UI 状态投影与检查点修复 |
| `server/services/checkpoint_sync.py` | `memory_pending_job`、`memory_processed_through`、`memory_submitted_through`、`memory_active_job` | 检查点同步标记 |

## 输入输出示例

`begin_turn` 的典型状态增量（服务端运行、首轮）：

```python
{
    "turn_id": "8b2f...（service_run_id 或 uuid4().hex）",
    "draft_reply": "", "draft_status": "pending", "draft_reasoning": "", "draft_usage": None,
    "service_reply_counted": False,
    "check_status": "pending", "check_issues": [], "check_reply": "",
    "check_feedback": "", "check_rounds": 0, "reply_error": "", "retry_message_id": "",
}
```

`commit_reply` 的典型增量（check 通过后）：

```python
{
    "messages": [AIMessage(id="reply_8b2f...", content="<timestamp>2026-09-23 12:00:00</timestamp>\n（点头）你好。",
                           usage_metadata={...})],
    "draft_reply": "", "draft_status": "committed", "check_reply": "",
    "draft_reasoning": "", "reply_error": "", "draft_usage": None,
}
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)
- 兄弟协议：[`../check/README.md`](../check/README.md)、[`../participant_state/README.md`](../participant_state/README.md)、[`../memory/README.md`](../memory/README.md)、[`../memory_job/README.md`](../memory_job/README.md)
- 协作模块：[`../../builder/README.md`](../../builder/README.md)、[`../../node/README.md`](../../node/README.md)、[`../../memory/README.md`](../../memory/README.md)
- 实现依据：[`agent/builder.py`](../../../../agent/builder.py)、[`agent/node/state.py`](../../../../agent/node/state.py)、[`agent/node/draft.py`](../../../../agent/node/draft.py)、[`agent/node/check.py`](../../../../agent/node/check.py)、[`agent/node/memory.py`](../../../../agent/node/memory.py)、[`agent/utils/memory.py`](../../../../agent/utils/memory.py)、[`server/services/agent.py`](../../../../server/services/agent.py)
- 测试覆盖（静态阅读交叉核对，未在本页重新执行）：`tests/test_rp_pipeline.py`（轮内字段与失败收尾）、`tests/test_reply_memory.py`（`messages` 工具消息保留、`iteration`）、`tests/test_participant_state.py`（状态合并与显式 `null`）、`tests/test_memory_service.py`、`tests/test_conversation_policy.py`（记忆字段）、`tests/test_event_judge.py`
- 未验证项：`AgentState` 无运行时类型校验，本页字段约束来自源码静态阅读。
