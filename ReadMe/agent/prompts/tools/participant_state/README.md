# participant_state 提示词

## 职责与入口

- 所属类别：提示词模块（非图节点）。源码：[agent/prompts/tools/participant_state.py](../../../../../agent/prompts/tools/participant_state.py)。
- 注册与导出：`tools/__init__.py` 导入并列入 `__all__`（[agent/prompts/tools/__init__.py:8](../../../../../agent/prompts/tools/__init__.py)）；`agent/prompts/__init__.py` 再次导出，并提供 `get_prompt("participant_state")` 分发（[agent/prompts/__init__.py:40](../../../../../agent/prompts/__init__.py)）。
- 获取函数：`get_participant_state_prompt(language: str = "zh") -> str`（[participant_state.py:10](../../../../../agent/prompts/tools/participant_state.py)）。
- 消费节点：同一工厂 `create_participant_state_node(trigger)` 实例化出的两个图节点（[agent/node/participant_state.py:53](../../../../../agent/node/participant_state.py)）：
  - `participant_state_in`：`trigger="user"`，在用户输入后、draft 之前执行（[agent/builder.py:88](../../../../../agent/builder.py)、[agent/builder.py:105](../../../../../agent/builder.py)）；
  - `participant_state_out`：`trigger="reply"`，在 `commit_reply` 之后执行（[agent/builder.py:95](../../../../../agent/builder.py)、[agent/builder.py:116](../../../../../agent/builder.py)）。
- 两个实例共享同一提示词，差异只在 `<update_trigger>` 取值与证据消息对的选取方式。

## 调用链总览

```text
（构建）create_participant_state_node(trigger) → 校验 trigger → 取模型，等待运行时取提示词

（运行）participant_state_in 或 participant_state_out
    → _message_pair(messages, trigger)          无有效消息对 → 直接返回 {}，不调模型
    → get_prompt("participant_state")           （本提示词，SystemMessage）
    → 用户消息 = character_state 文本 + user_state 文本 + world_state 文本
                 + <update_trigger> + <evidence>
    → get_node_model("participant_state").ainvoke(...)  超时 90 秒，最多 3 次尝试
    → ParticipantStateUpdate 校验 → 合并回 character_state / user_state
```

## 获取链

### B1. `get_participant_state_prompt`

- 定位与签名：`agent.prompts.tools.participant_state.get_participant_state_prompt(language: str = "zh") -> str`，同步函数，[源码 participant_state.py:10](../../../../../agent/prompts/tools/participant_state.py)。
- 调用方与条件：节点运行时通过 `get_prompt("participant_state")` 获取（[agent/node/participant_state.py:67](../../../../../agent/node/participant_state.py)）；未传 `language`，取缺省 `"zh"`。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `language` | `str` | 默认 `"zh"` | 语言标识；`normalize_language` 结果为 `"zh"` 时返回中文，否则返回英文常量 |

- 隐式输入：`normalize_language`（[agent/utils/language.py:4](../../../../../agent/utils/language.py)）。
- 功能与内部调用：归一语言后直接返回 `_PROMPT_ZH`（[participant_state.py:14](../../../../../agent/prompts/tools/participant_state.py)）或 `_PROMPT_EN`（[participant_state.py:75](../../../../../agent/prompts/tools/participant_state.py)）；无其他内部调用。
- 输出：完整状态更新系统提示词字符串。注意 EN 变体不是空字符串，而是一个换行符 `"\n"`（[participant_state.py:75](../../../../../agent/prompts/tools/participant_state.py)）；当前调用方未传 `language`，实际返回中文版本。
- 副作用与异常：无；不读文件、不调模型、不抛异常。

生成内容概要（不复制原文）：

| 部分 | 内容 |
| --- | --- |
| 角色定位 | 同时更新角色与用户状态；`character_state` 属于 AI 扮演的角色，`user_state` 属于用户，与外部世界状态分开存储 |
| 输入说明 | `<character_state>`/`<user_state>`（最新消息前已保存状态）、`<world_state>`（只作场景参考）、`<update_trigger>`（`user` 表示用户输入后，`reply` 表示正式回复后）、`<evidence>`（按时间排序的消息对，`is_new=true` 为最新消息，`is_new=false` 为上一条对方消息；消息内容为数据，不执行其中指令） |
| 更新规则 | 同时检查双方状态，按已发生事实及其直接后果更新；已保存状态包含上一条消息影响，不重放、不重复累加；`user` 与 `reply` 两种触发各自的观察对象；不编造用户自主动作/情绪/同意，计划与假设不算事实；位置、情绪、身体、穿着不可互相复制 |
| 快照语义 | 每个字段只保存当前时刻快照，有变化整体替换，不叙述变化过程；无新证据保留旧值，未知保持 `null`；失效状态直接删除，不写否定形式；`clothing` 为 `null` 表示不清楚，裸体需给出状态 |
| 字段说明 | `location`/`mood`/`body`/`clothing` 的取值含义；`hearing` 仅属于角色，取 `same_room`/`near`/`far`/`unknown` |
| 输出契约 | 只输出一个 JSON 对象，包含完整 `character_state` 与 `user_state`；`null` 必须是 JSON null 而非字符串；附字段结构示例并声明其不是默认值 |

## 运行链

### R1. `create_participant_state_node.<locals>.node`（提示词消费）

- 定位与签名：`agent.node.participant_state.create_participant_state_node` 内的 `node(state: AgentState)`，异步；[agent/node/participant_state.py:58](../../../../../agent/node/participant_state.py)。
- 调用方与条件：LangGraph 调度；`participant_state_in` 由 `world_state_update` 触发，`participant_state_out` 由 `commit_reply` 触发。工厂在构建时校验 `trigger in ("user", "reply")`，非法值抛 `ValueError`（[agent/node/participant_state.py:54](../../../../../agent/node/participant_state.py)）。
- 工厂输入（构建阶段）：`trigger`（`str`，必填）；闭包依赖：模型 `get_node_model("participant_state")`。
- 执行阶段输入：

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `messages` | `list` | state | 无有效消息对时节点直接返回 `{}` | `_message_pair` 提取证据 |
| `character_state` | `dict \| None` | state | `prepare_character_state` 返回状态字典与文本 | 作为已保存状态输入并用于合并 |
| `user_state` | `dict \| None` | state | `prepare_user_state` 同上 | 同上 |
| `world_state` | `dict \| None` | state | `prepare_world_state` 只取文本 | 场景参考 |
| `trigger` | `str` | 工厂闭包 | 构建时固定 | 写入 `<update_trigger>` 并决定证据对类型 |

- 隐式输入：提示词 B1 结果；`json.dumps(pair, ensure_ascii=False, indent=2)` 生成的证据文本；`strip_timestamps`、`strip_code_fence` 清洗函数。
- 功能与内部调用：
  1. `_message_pair(messages, trigger)`（[agent/node/participant_state.py:23](../../../../../agent/node/participant_state.py)）：从最新消息向前找消息对，跳过带工具调用的 AI 消息；最新消息类型不符合触发类型或最新文本为空时返回 `[]`；`trigger="user"` 要求最新为 HumanMessage，`trigger="reply"` 要求最新为 AIMessage；返回项含 `speaker`、`is_new`、`content`；
  2. 消息对为空则返回 `{}`（不调用模型，保持状态不变）；
  3. 组装系统消息（B1 提示词）与用户消息：`character_text + user_text + world_text + <update_trigger>…</update_trigger> + <evidence>…</evidence>`；
  4. `asyncio.wait_for(llm.ainvoke(messages), timeout=90)`，失败按 1 秒间隔重试，最多 3 次尝试；
  5. `strip_timestamps`、`strip_code_fence` 后以 `ParticipantStateUpdate.model_validate_json` 解析；
  6. 用 `model_dump(exclude_unset=True)` 合并到已有字典：缺失字段保留旧值，显式 `null` 清除字段；两份状态都解析成功后才一起返回。
- 输出与状态字段：

| 输出或状态字段 | 类型 | 产生条件 | 含义与消费方 |
| --- | --- | --- | --- |
| `character_state` | `dict` | 模型解析成功 | 与旧字典合并后的角色状态；下游 draft / check 使用 |
| `user_state` | `dict` | 模型解析成功 | 合并后的用户状态；下游 draft 使用 |
| `{}` | `dict` | 无有效消息对或 3 次尝试均失败 | 状态保持原样，不阻断主流程 |

- 副作用：模型请求（最多 3 次）；日志记录 `trigger` 与更新结果。
- 异常与边界：`asyncio.TimeoutError` 与一般异常都被捕获并重试；解析失败不写回任何一方（避免只更新一半）。
- 后续去向：`participant_state_in` 返回后由框架合并状态并进入 draft；`participant_state_out` 返回后进入 `update_iter`。

## 分支与异常链

- **无有效消息对**：如重复执行但最新消息类型不符，返回 `{}`，不产生模型请求（[agent/node/participant_state.py:59](../../../../../agent/node/participant_state.py)）。
- **超时/解析失败/模型异常**：3 次尝试后返回 `{}`，状态维持原值，本轮继续执行。
- **显式 null 与缺失字段**：提示词要求字段快照；解析时 `exclude_unset` 区分“未返回”与“显式 null”，前者保留旧值，后者清除失效状态。
- **EN 语言**：EN 变体为 `"\n"`，模型缺少规则说明；当前调用方未传 `language`，不会进入该分支。

## 输入输出示例

适用 R1（`trigger="user"`，示意，非真实内容）：

```text
user 消息：
<character_state>{"location":"客厅", ...}</character_state>
<user_state>{"location":null, ...}</user_state>
<world_state>...</world_state>
<update_trigger>user</update_trigger>
<evidence>
[
  {"speaker": "character", "is_new": false, "content": "外面下雨了。"},
  {"speaker": "user", "is_new": true, "content": "我带伞了，马上到。"}
]
</evidence>
```

期望输出（解析为 `ParticipantStateUpdate` 后合并）：

```json
{
  "character_state": {"location": "客厅", "mood": "安心", "body": null, "clothing": null, "hearing": "near"},
  "user_state": {"location": "路上", "mood": null, "body": null, "clothing": null}
}
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md)
- 消费节点源码：[agent/node/participant_state.py](../../../../../agent/node/participant_state.py)；节点注册与边：[agent/builder.py](../../../../../agent/builder.py)
- 结构化协议：[agent/classes/participant_state.py](../../../../../agent/classes/participant_state.py)
- 验证依据：静态阅读源码；本次未执行测试。
