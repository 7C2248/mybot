# 图节点

`agent/node/` 实现注册进 LangGraph 的全部图节点及路由函数。节点负责单步状态更新或模型交互，路由函数只读取状态并返回下一节点名；两者都由 [agent/builder.py](../../../agent/builder.py) 注册（见 [../builder/README.md](../builder/README.md)）。

本页只说明有哪些节点、各自功能与关系；逐函数输入输出下沉到各叶子 README。

## 节点清单

| 节点或子目录 | 类型 | 主要功能 | 上下游 | 源码 | 详细文档 |
| --- | --- | --- | --- | --- | --- |
| `state` | 图节点 ×2 | 轮初重置/重试去重（`begin_turn`）、成功轮数计数（`increment_iteration`） | 入口；`apply_memory_results`；`update_iter` 后接 `tts` | [state.py](../../../agent/node/state.py) | [state/README.md](state/README.md) |
| `context` | 图节点 | 按 token 预算与消息数裁剪模型上下文，隐藏关闭检索后的记忆工具消息 | `apply_memory_results` → `world_state_update` | [context.py](../../../agent/node/context.py) | [context/README.md](context/README.md) |
| `world_state` | 图节点 | 系统时间 + 和风天气实时天气写入 `world_state` | `limit_context` → `participant_state_in` | [world_state.py](../../../agent/node/world_state.py) | [world_state/README.md](world_state/README.md) |
| `participant_state` | 节点工厂（两实例） | 用一次模型调用共同更新角色与用户状态；`participant_state_in`（trigger=user）与 `participant_state_out`（trigger=reply）共享工厂 | `world_state_update` → `draft`；`commit_reply` → `update_iter` | [participant_state.py](../../../agent/node/participant_state.py) | [participant_state/README.md](participant_state/README.md) |
| `draft` | 节点工厂 + 路由 + 执行函数 | 生成候选回复、绑定工具、`draft_judge` 路由、`commit_reply` 正式提交、`reply_failed` 失败收尾 | `participant_state_in` → `draft` ⇄ `tools` → `check` | [draft.py](../../../agent/node/draft.py) | [draft/README.md](draft/README.md) |
| `check` | 节点工厂 + 路由 | 本地规则 + 模型检查候选回复；`check_judge` 路由到提交/修订/失败 | `draft` → `check` → `commit_reply`/`draft`/`reply_failed` | [check.py](../../../agent/node/check.py) | [check/README.md](check/README.md) |
| `tts` | 节点工厂 | 解析对白、生成语气指令并合成/播放语音（`need_tts` 关闭时跳过） | `update_iter` → `tts`，条件边到 `prepare_memory`/`END` | [tts.py](../../../agent/node/tts.py) | [tts/README.md](tts/README.md) |
| `event` | 路由函数 | `event_judge`：是否进入记忆处理 | `tts` 的条件边 | [event.py](../../../agent/node/event.py) | [event/README.md](event/README.md) |
| `memory` | 节点工厂 ×3 | `prepare_memory` 生成快照、`enqueue_memory` 入队、`apply_memory_results` 轮初应用结果 | `tts` → `prepare_memory` → `enqueue_memory` → `END`；`begin_turn` → `apply_memory_results` | [memory.py](../../../agent/node/memory.py) | [memory/README.md](memory/README.md) |
| `tools`（无源码文件） | LangGraph `ToolNode` | 执行主模型发起的工具调用，结果作为 `ToolMessage` 回到 `messages` | `draft` → `tools` → `draft` | [agent/builder.py](../../../agent/builder.py) | [../tools/README.md](../tools/README.md) |

说明：

- `state`、`world_state`、`context`、`draft`、`check`、`tts`、`event`、`memory` 均为图节点或路由；`participant_state` 是“同一工厂实例化为两个节点”的例子，必须区分 `trigger` 差异。
- `tools` 是框架预置节点，其可用工具集由 [../tools/README.md](../tools/README.md) 定义。
- 各节点内部还会调用 `agent/utils/`、`agent/prompts/`、`agent/classes/` 中的支撑模块，不在本页展开。

## 整体流程

```text
begin_turn → apply_memory_results → limit_context → world_state_update
  → participant_state_in → draft

draft --draft_judge: tools--> tools → draft
draft --draft_judge: check--> check
draft --draft_judge: reply_failed--> reply_failed → END

check --check_judge: draft--> draft
check --check_judge: commit_reply--> commit_reply
check --check_judge: reply_failed--> reply_failed → END

commit_reply → participant_state_out → update_iter → tts
tts --event_judge True--> prepare_memory → enqueue_memory → END
tts --event_judge False--> END
```

- 轮初：`begin_turn` 重置本轮字段；`apply_memory_results` 同步后台记忆结果与入队；`limit_context` 裁剪上下文；`world_state_update` 刷新时间/天气；`participant_state_in` 更新双方状态。
- 生成：`draft` 可循环调用 `tools`；候选交给 `check`，通过后由 `commit_reply` 提交，未通过回到 `draft`，轮数耗尽或服务不可用进入 `reply_failed`。
- 收尾：`participant_state_out` 再次更新状态，`update_iter` 计数，`tts` 合成语音，`event_judge` 决定是否投递记忆任务。

## 主要数据与依赖

- **候选与正式回复**：`draft_reply`/`draft_status`/`draft_reasoning`/`draft_usage` 只在本轮流转；`commit_reply` 生成 `AIMessage(id="reply_{turn_id}")` 并清空候选字段。字段定义见 [../classes/state/README.md](../classes/state/README.md)。
- **检查状态**：`check_status`/`check_issues`/`check_feedback`/`check_rounds`/`check_reply` 由 `check` 写入、`draft` 读取修订意见。
- **双方状态**：`world_state`/`character_state`/`user_state` 由三个状态节点更新，经 [agent/utils/state.py](../../../agent/utils/state.py) 格式化为提示词文本。
- **记忆交接**：`memory_pending_job`/`memory_active_job`/`memory_processed_through` 等跨轮保留；快照与裁剪工具见 [../utils/memory/README.md](../utils/memory/README.md)。
- **模型与外部服务**：节点模型来自 [agent/utils/models.py](../../../agent/utils/models.py)（`get_node_model`）；天气来自 [agent/utils/weather.py](../../../agent/utils/weather.py)；TTS 权重来自本地 Qwen 模型。

## 阅读导航

- 上级：[Agent 总览](../README.md) · 构建：[../builder/README.md](../builder/README.md)
- 协议：[../classes/README.md](../classes/README.md) · 工具：[../tools/README.md](../tools/README.md) · 提示词：[../prompts/README.md](../prompts/README.md)
- 后台记忆：[../memory/README.md](../memory/README.md)
