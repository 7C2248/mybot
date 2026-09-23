# participant_state 节点（角色与用户状态联合更新）

## 职责与入口

- 所属类别：图节点工厂，一次模型调用同时更新 `character_state` 与 `user_state`。
- 源码：[agent/node/participant_state.py](../../../../agent/node/participant_state.py)
- 图注册名（同一工厂两个实例）：
  - `participant_state_in` = `create_participant_state_node(trigger="user")`：用户输入后执行，上游 `world_state_update`，下游 `draft`。
  - `participant_state_out` = `create_participant_state_node(trigger="reply")`：正式回复后执行，上游 `commit_reply`，下游 `update_iter`。
- 触发时机：每轮两次（成功提交时）。工厂在构建期创建模型对象，执行函数每轮读取状态。

## 调用链总览

| 阶段 | 步骤 | 关系 |
| --- | --- | --- |
| 构建 | B1 `create_participant_state_node(trigger)` | 工厂：校验 trigger、获取节点模型 |
| 运行 | R1 `node(state)` | 图调度（两个实例分别触发） |
| 运行 | R1.1 `_message_pair(messages, trigger)` | 内部函数调用 |
| 运行 | R1.2 `prepare_character_state` / `prepare_user_state` / `prepare_world_state` | 直接函数调用 |
| 运行 | R1.3 `llm.ainvoke(messages)` | 异步模型请求（带超时/重试） |
| 运行 | R1.4 `ParticipantStateUpdate.model_validate_json` | 协议校验（[../../classes/participant_state/README.md](../../classes/participant_state/README.md)） |

## 构建链

### B1. `create_participant_state_node`

- 定位与签名：`create_participant_state_node(trigger: str = "user")`，同步工厂，返回 `async def node(state)`，[agent/node/participant_state.py:53](../../../../agent/node/participant_state.py#L53)。
- 调用方与条件：`build_rp_agent` 在注册 `participant_state_in`/`participant_state_out` 时调用（[agent/builder.py](../../../../agent/builder.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `trigger` | `str` | 默认 `"user"`；只允许 `"user"`/`"reply"` | 区分两个实例：处理最新用户消息还是最新正式回复 |

功能：校验 `trigger`，非法值抛 `ValueError("[ParticipantState] 未知 trigger: ...")`；调用 `get_node_model("participant_state")` 获取模型（[../../utils/models/README.md](../../utils/models/README.md)），闭包缓存。

输出：执行函数 `node`；`trigger` 与模型实例被闭包捕获。

副作用、异常与去向：构建期非法 `trigger` 立即抛错；模型实例按配置缓存，运行阶段不再重建。

## 运行链

### R1. `node(state)`（两个实例共享）

- 定位与签名：`create_participant_state_node.<locals>.node(state: AgentState)`，异步函数，[agent/node/participant_state.py:58](../../../../agent/node/participant_state.py#L58)。
- 调用方与条件：LangGraph 调度；`trigger="user"` 实例在用户输入后执行，`trigger="reply"` 实例在 `commit_reply` 后执行。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `messages` | `list` | checkpoint | 缺省 `[]` | 提取最新消息对（R1.1） |
| `character_state` | `dict` | checkpoint | 可为 `None` | 旧角色状态基线 |
| `user_state` | `dict` | checkpoint | 可为 `None` | 旧用户状态基线 |
| `world_state` | `dict` | 本轮 `world_state_update` | 可为 `None` | 提示词场景参考 |

隐式输入：闭包中的 `trigger` 与节点模型；`get_prompt("participant_state")`（[../../prompts/tools/participant_state/README.md](../../prompts/tools/participant_state/README.md)）；常量 `_STATE_TIMEOUT=90`、`_STATE_MAX_RETRIES=2`。

功能与内部调用：

1. `pair = _message_pair(messages, trigger)`（R1.1）；空列表直接返回 `{}`（没有新输入/新回复时不重复处理旧消息）。
2. 分别调用 `prepare_character_state`、`prepare_user_state`、`prepare_world_state`（R1.2）得到旧状态字典与提示词文本。
3. 组装消息：system 为 `participant_state` 提示词；user 内容为 `character_text + user_text + world_text`，加 `<update_trigger>{trigger}</update_trigger>` 与 `<evidence>`（JSON 格式的消息对，`ensure_ascii=False`）。
4. 最多 3 次尝试（`_STATE_MAX_RETRIES + 1`）：`asyncio.wait_for(llm.ainvoke(messages), timeout=90)`（R1.3）；响应文本经 `strip_timestamps`、`strip_code_fence` 清洗后用 `ParticipantStateUpdate.model_validate_json` 校验（R1.4）。
5. 校验成功后，把 `update.character_state`/`update.user_state` 的 `model_dump(exclude_unset=True)` 合并到旧状态上，**两份状态全部验证成功后一起返回**；失败/超时记录 warning，间隔 1 秒重试；全部失败返回 `{}`。

### R1.1. `_message_pair`

- 定位与签名：`_message_pair(messages: list, trigger: str) -> list[dict]`，同步私有函数，[agent/node/participant_state.py:23](../../../../agent/node/participant_state.py#L23)。
- 输入：完整消息列表与 `trigger`。
- 行为：
  - `latest_type` 为 `HumanMessage`（user）或 `AIMessage`（reply）。
  - 反向遍历，跳过非 `HumanMessage`/`AIMessage`；跳过带 `tool_calls` 或 `invalid_tool_calls` 的 `AIMessage`。
  - 第一条有效消息必须是 `latest_type`，否则返回 `[]`（表示没有新的用户输入/正式回复）；随后跳过同类型消息，取最近的另一方消息。
  - 文本经 `strip_timestamps(content_text(...)).strip()`；最新消息为空则返回 `[]`，对方消息为空则跳过。
  - 输出按时间顺序的 1~2 项：`[{"speaker": "user"|"character", "is_new": bool, "content": str}]`；`is_new=True` 表示本次最新消息。
- 输出用途：作为 `<evidence>` 交给状态更新模型，避免把旧消息当新事件重放。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `character_state` | `dict` | 模型校验成功 | 旧状态 + 模型提供的字段（未提供字段保留；显式 `null` 清除） | `draft`、服务端 `state.updated` |
| `user_state` | `dict` | 模型校验成功 | 同上 | `draft`、服务端 `state.updated` |

副作用：发起一次（可能重试的）模型请求；成功/失败均写日志。

异常与边界：

- 超时（90 秒）或解析失败：重试至多 3 次，最终返回 `{}`，本轮状态保持旧值，不阻塞对话。
- 模型输出缺字段时保留旧值；显式 `null` 用于清除已失效状态（协议 `strict=True`、`extra="forbid"`）。
- 只有一方校验通过时不会部分写回，避免角色/用户状态不一致。

后续去向：`participant_state_in` 返回后由框架合并，固定边到 `draft`；`participant_state_out` 返回后固定边到 `update_iter`。

## 分支与异常链

- **无新消息**（`_message_pair` 返回 `[]`）：直接返回 `{}`，不调用模型。
- **模型失败/超时**：3 次尝试后返回 `{}`，本轮其余流程继续。
- **同一轮多次调度**：图只注册两个实例，各自最多执行一次；重试运行由 `begin_turn` 重置字段后重新触发。

## 输入输出示例

适用 R1（trigger=user）：

```text
输入：messages 末尾 = [HumanMessage("我把外套脱了")]，
      character_state = {"location":"客厅","clothing":"外套"}，user_state = {...}
输出：{"character_state": {"location":"客厅","clothing":null,...},
       "user_state": {...}}   # 角色未受影响字段保留；仅模型显式给出的字段变化
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 协议：[../../classes/participant_state/README.md](../../classes/participant_state/README.md) · 提示词：[../../prompts/tools/participant_state/README.md](../../prompts/tools/participant_state/README.md)
- 状态格式化：[../../utils/state/README.md](../../utils/state/README.md)
- 依据：`agent/node/participant_state.py`；`tests/test_participant_state.py` 覆盖消息对提取与合并规则；本次未执行测试。
