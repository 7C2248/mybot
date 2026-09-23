# state 节点（轮初重置与迭代计数）

## 职责与入口

- 所属类别：图节点（无工厂，普通同步函数）。
- 源码：[agent/node/state.py](../../../../agent/node/state.py)
- 图注册名：`begin_turn` → `begin_turn`，`update_iter` → `increment_iteration`（[agent/builder.py](../../../../agent/builder.py)）。
- 上游/下游：`begin_turn` 是图入口，固定边到 `apply_memory_results`；`increment_iteration` 由 `participant_state_out` 调度，固定边到 `tts`。
- 触发时机：每一轮开始/成功收尾各执行一次。

## 调用链总览

| 步骤 | 函数 | 关系 |
| --- | --- | --- |
| R1 | `begin_turn(state)` | 图调度（入口节点） |
| R1.1 | `_retry_input_content(message)` | `begin_turn` 内部调用 |
| R2 | `increment_iteration(state)` | 图调度（`update_iter` 节点） |

本模块没有工厂和构建阶段，节点函数在构建期直接注册。

## 运行链

### R1. `begin_turn`

- 定位与签名：`begin_turn(state: AgentState)`，同步函数，[agent/node/state.py:33](../../../../agent/node/state.py#L33)。
- 调用方与条件：LangGraph 作为入口节点调度；每次运行开始时无条件执行。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `service_run_id` | `str` | 服务端初始输入 | 可缺省（空串/无） | 作为本轮 `turn_id`；CLI 旧路径无此值时生成 UUID |
| `retry_message_id` | `str` | 上一轮 `reply_failed` 输出 | 缺省 `""` | 失败重试时定位需要去重的用户输入 |
| `reply_error` | `str` | 上一轮 `reply_failed` 输出 | 缺省 `""` | 非空表示上一轮失败，才尝试重试去重 |
| `messages` | `list` | checkpoint | 缺省 `[]` | 查找重试输入与重复提交 |

隐式输入：`uuid.uuid4()`（无 `service_run_id` 时生成 `turn_id`）；日志器 `node.state`。

功能与内部调用：

1. 构造本轮重置增量：`turn_id`（优先 `service_run_id`，否则 `uuid4().hex`）、`draft_reply=""`、`draft_status="pending"`、`draft_reasoning=""`、`draft_usage=None`、`service_reply_counted=False`、`check_status="pending"`、`check_issues=[]`、`check_reply=""`、`check_feedback=""`、`check_rounds=0`、`reply_error=""`、`retry_message_id=""`。
2. 若上一轮 `reply_error` 非空且存在 `retry_message_id`：在 `messages` 中查找 ID 匹配且类型为 `HumanMessage` 的消息下标（R1.1）；找不到则不做任何删除。
3. 找到后取该消息之后的所有消息，仅当它们**全部是用户消息**且去掉开头时间戳后的内容与该重试输入一致时，才生成 `RemoveMessage` 列表删除重复提交；否则保留。

### R1.1. `_retry_input_content`

- 定位与签名：`_retry_input_content(message: HumanMessage)`，同步私有函数，[agent/node/state.py:25](../../../../agent/node/state.py#L25)。
- 输入：用户消息；`content` 为字符串时去除开头的 `<timestamp>...</timestamp>`（含可选换行），其他类型原样返回。
- 输出：用于比较的文本；`begin_turn` 用它判断重复提交是否同一输入。
- 目的：CLI/服务端每轮都会给用户输入加时间戳，比较时只忽略开头时间戳，不按内容全局去重。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `turn_id` | `str` | 总是 | 本轮唯一 ID，`commit_reply` 用它生成 `reply_{turn_id}` | `commit_reply`、日志 |
| 候选/检查重置字段 | 各类型 | 总是 | 覆盖上一轮残留，避免跨轮污染 | 本轮 `draft`/`check` |
| `messages` | `list[RemoveMessage]` | 仅重试且存在重复提交 | `add_messages` reducer 显式删除重复用户消息 | 后续节点读取的 `messages` |

副作用：仅记录一条“重试复用用户输入”日志；不修改数据库、不调用模型。

异常与边界：无显式异常处理；`state` 缺字段时用 `.get` 回退。若重复消息中间夹有非用户消息则完全不去重（宁可多留消息，也不误删）。

后续去向：返回增量由框架合并；下一节点 `apply_memory_results`。

### R2. `increment_iteration`

- 定位与签名：`increment_iteration(state: AgentState)`，同步函数，[agent/node/state.py:16](../../../../agent/node/state.py#L16)。
- 调用方与条件：图注册名 `update_iter`，由 `participant_state_out` 固定边调度；只在正式回复已提交后执行。

| 输入字段 | 类型 | 来源 | 缺省 | 用途 |
| --- | --- | --- | --- | --- |
| `iteration` | `int` | checkpoint | `0` | 加一得到累计成功轮数 |
| `service_run_id` | `str` | 初始输入 | 空 | 非空时标记本回复已计数 |

功能：`iteration = state.get("iteration", 0) + 1`；若 `service_run_id` 非空，同时返回 `service_reply_counted=True`，供服务端 checkpoint 修复时判断是否需要补计。

| 输出或状态字段 | 类型 | 产生条件 | 含义 | 消费方 |
| --- | --- | --- | --- | --- |
| `iteration` | `int` | 总是 | 成功对话轮数，`event_judge` 的轮数上下限依据 | `event_judge`、记忆快照 |
| `service_reply_counted` | `bool` | `service_run_id` 非空 | 本轮已计入 iteration | `AgentAdapter.repair` |

副作用：无（仅状态增量）。异常与边界：无；不校验 `iteration` 类型。

后续去向：固定边到 `tts`。

## 分支与异常链

- **失败重试**：上一轮 `reply_failed` 已设置 `reply_error`/`retry_message_id`；本轮 `begin_turn` 删除用户输入后的重复提交，用户只保留一份原文，`draft` 重新生成。
- **无重复提交**：`repeated` 为空时保留全部消息，日志记录“无需删除”。
- **找不到用户输入**：不修改 `messages`，仅重置字段。
- **非服务路径**：`service_run_id` 为空时 `turn_id` 随机生成，`service_reply_counted` 保持 `False`。

## 输入输出示例

适用 R1（失败重试分支）：上一轮状态含 `reply_error="...请重新提交该输入重试。"`、`retry_message_id="user_42"`，`messages` 末尾为用户再次提交的同一文本。

```text
输入增量：
  retry_message_id = "user_42"
  messages[.., HumanMessage(id="user_42", content="<timestamp>...</timestamp>\n早安"),
                 HumanMessage(id="user_57", content="<timestamp>...</timestamp>\n早安")]
输出：
  { "turn_id": "<service_run_id>", "draft_status": "pending", ...,
    "messages": [RemoveMessage(id="user_57")] }
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 下游：[../memory/README.md](../memory/README.md)（`apply_memory_results`）
- 协议：[../../classes/state/README.md](../../classes/state/README.md)
- 依据：`agent/node/state.py`；`tests/test_rp_pipeline.py`、`tests/test_participant_state.py` 等离线测试涉及轮初行为；本次未执行测试。
