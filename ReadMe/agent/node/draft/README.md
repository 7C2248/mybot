# draft 节点（主回复、路由、正式提交与失败收尾）

## 职责与入口

- 所属类别：图节点工厂 + 路由函数 + 执行函数。
- 源码：[agent/node/draft.py](../../../../agent/node/draft.py)
- 图注册名（[agent/builder.py](../../../../agent/builder.py)）：

| 图注册名 | 实现 | 类型 | 触发方式 |
| --- | --- | --- | --- |
| `draft` | `create_draft_node(...)` 返回的 `node` | 模型节点 | `participant_state_in`、`tools`、`check` 的边 |
| `draft_judge` | `draft_judge` | 条件路由 | `draft` 的条件边 |
| `commit_reply` | `commit_reply` | 执行函数 | `check_judge` 返回 `"commit_reply"` |
| `reply_failed` | `reply_failed` | 执行函数 | `draft_judge`/`check_judge` 返回 `"reply_failed"` |

本模块是唯一产生正式角色回复的位置：候选只在 `draft_reply` 中流转，提交前必须满足检查条件。

## 调用链总览

```text
构建：B1 create_draft_node ──返回──> node
运行：R1 node（生成候选或工具调用）
      ├─ R1.1 _extract_reply（提取 <reply> 正文）
      └─ R1.2 _usage_dict（token 用量转 dict）
      R2 draft_judge（路由）
      R3 commit_reply（正式提交，check 通过后）
      R4 reply_failed（失败收尾）
```

图调度关系：`draft` 的结果经 `draft_judge` 决定进入 `tools`、`check` 或 `reply_failed`；`commit_reply` 与 `reply_failed` 由 `check` 的路由到达，不由 `node` 直接调用。

## 构建链

### B1. `create_draft_node`

- 定位与签名：`create_draft_node(character_name: str, language: str = "zh", character_profile: str | None = None, tools: list[BaseTool] | None = None)`，同步工厂，返回 `async def node(state)`，[agent/node/draft.py:43](../../../../agent/node/draft.py#L43)。
- 调用方与条件：`build_rp_agent` 注册 `draft` 时调用。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `character_name` | `str` | 必填 | 角色名，用于生成提示词 |
| `language` | `str` | 默认 `"zh"` | 提示词语言，经 `normalize_language` 归一 |
| `character_profile` | `str` 或 `None` | 默认 `None` | 角色档案文本；为 `None` 时提示词内部自行读取 |
| `tools` | `list[BaseTool]` 或 `None` | 默认 `None` | 默认工具集；非空时绑定到模型 |

功能：

1. `tools = list(tools or [])`；`llm = get_node_model("main")`。
2. 有工具时 `llm.bind_tools(tools, parallel_tool_calls=True)`，允许一次返回多个工具调用。
3. `prompt_base = get_draft_prompt(character_name, language, character_profile)`；EN 语言时该提示词为空串（见 [../../prompts/main/draft/README.md](../../prompts/main/draft/README.md)）。
4. 闭包捕获 `tools`、`llm`、`prompt_base` 供执行阶段使用。

输出：执行函数 `node`。副作用：构建期读取模型配置与角色档案；异常向上抛出。

## 运行链

### R1. `node(state)`

- 定位与签名：`create_draft_node.<locals>.node(state: AgentState)`，异步函数，[agent/node/draft.py:57](../../../../agent/node/draft.py#L57)。
- 调用方与条件：LangGraph 调度；首次由 `participant_state_in` 到达，工具循环中由 `tools` 到达，检查未通过时由 `check` 到达。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `messages` | `list` | checkpoint | 缺省 `[]` | 作为模型消息输入（含 ToolMessage 检索原文） |
| `world_state` | `dict` | `world_state_update` | 可为 `None` | 拼入 system 的 `<world_state>` |
| `character_state` | `dict` | `participant_state_in/out` | 可为 `None` | 拼入 system 的 `<character_state>` |
| `user_state` | `dict` | `participant_state_in/out` | 可为 `None` | 拼入 system 的 `<user_state>` |
| `check_feedback` | `str` | `check` 失败输出 | 缺省 `""` | 非空时进入修订模式 |
| `check_issues` | `list[dict]` | `check` 失败输出 | 缺省 `[]` | 判断是否属于拒答问题（不注入上一版草稿） |
| `draft_reply` | `str` | 上一轮 `node` 输出 | 缺省 `""` | 修订时作为 `<previous_draft>` |

隐式输入：闭包中的模型、工具与提示词；`utils.time.get_time`（仅 `commit_reply`）；`_DRAFT_MAX_RETRIES=3`；日志器 `node.draft`。

功能与内部调用：

1. 读取 `messages`；调用 `prepare_world_state`/`prepare_character_state`/`prepare_user_state` 得到三段状态文本，拼成 `system_content = prompt_base + "\n" + world + character + user`。
2. 若工具集中没有 `memory_query`，追加说明“当前未提供记忆检索工具……缺少依据时保留未知”，防止模型假称已检索。
3. 修订模式：`check_feedback` 非空时：
   - 若 `check_issues` 中不存在 `type == "refusal"` 的问题，注入 `<previous_draft>上一版候选</previous_draft>`；
   - 追加 `<check_feedback>...</check_feedback>` 与“请修正指出的问题，重新生成完整候选回复”。
4. 每次尝试先构造 `reset_check`（`check_status="pending"`、`check_issues=[]`、`check_reply=""`、`draft_usage=None`、`draft_reasoning=""`），然后最多 4 次（`_DRAFT_MAX_RETRIES + 1`）调用 `llm.ainvoke([SystemMessage(system_content)] + messages)`：
   - `response.invalid_tool_calls` 非空 → 抛 `ValueError`；
   - `response.tool_calls` 非空：校验调用 ID 存在、唯一，且工具名都在当前工具集中；不合法抛 `ValueError`；合法则返回 `dict(reset_check, messages=[response], draft_status="calling_tools")`，由 `draft_judge` 路由到 `tools`；
   - 否则提取文本：`content_text(response.content)`；正文为空时尝试从 `reasoning_content` 中 `</thinking_process>` 之后截取；
   - `reply = strip_timestamps(_extract_reply(content))`（R1.1）；空正文和拒答不在此拦截，交给 `check`；
   - 成功返回 `draft_reply=reply`、`draft_status="ready"`、`draft_reasoning`、`draft_usage=_usage_dict(usage_metadata)`（R1.2）。
   - 失败记录 warning，间隔 1 秒重试。
5. 全部尝试失败：记录“draft 重试耗尽”并返回 `dict(reset_check, draft_reply="", draft_status="failed")`。

### R1.1. `_extract_reply`

- 定位与签名：`_extract_reply(content: str) -> str`，同步私有函数，[agent/node/draft.py:23](../../../../agent/node/draft.py#L23)。
- 行为：先移除完整的 `<thinking_process>`/`<monologue>` 内部容器；未闭合容器之后的内容全部丢弃；再提取 `<reply>...</reply>` 内部文本；没有 `<reply>` 容器时返回清洗后的全文；最后 `strip()`。
- 目的：兼容旧版思考容器格式，确保内部预演不会进入正式正文。

### R1.2. `_usage_dict`

- 定位与签名：`_usage_dict(usage) -> dict | None`，同步私有函数，[agent/node/draft.py:34](../../../../agent/node/draft.py#L34)。
- 行为：`None` 返回 `None`；有 `model_dump` 用 `exclude_none=True` 转换；否则过滤 `None` 值后返回普通 dict。
- 目的：把 LangChain `usage_metadata` 转成可被 checkpoint 序列化的纯 dict。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `messages` | `list[AIMessage]` | 模型返回工具调用 | 追加到消息队列（`add_messages`） | `tools` 节点 |
| `draft_status` | `str` | 总是 | `"calling_tools"`/`"ready"`/`"failed"` | `draft_judge` |
| `draft_reply` | `str` | 生成成功 | 清洗后的候选正文 | `check`、修订注入 |
| `draft_reasoning` | `str` | 生成成功 | 模型思维链（`reasoning_content`） | `check` 拒答规则、`commit_reply` 元数据 |
| `draft_usage` | `dict` 或 `None` | 生成成功 | 最后一次生成的 token 用量 | `commit_reply` 写入消息 |
| `check_status`/`check_issues`/`check_reply` | 重置值 | 每次进入 | 覆盖上一轮检查残留 | `check` |

副作用：模型请求（可能 1~4 次）；仅成功/失败日志。

异常与边界：

- 工具调用参数不可解析（`invalid_tool_calls`）、ID 缺失/重复、工具名不可用：按失败重试，最终进入 `reply_failed`。
- 不设置单次调用超时，等待模型或由调用方取消（服务停止时执行器取消任务）。
- `reasoning_content` 中未闭合容器后的文本不进入正文；`<reply>` 缺失时退化为全文清洗结果。

后续去向：由 `draft_judge`（R2）路由。

### R2. `draft_judge`

- 定位与签名：`draft_judge(state: AgentState) -> str`，同步路由，[agent/node/draft.py:114](../../../../agent/node/draft.py#L114)。

| 条件 | 返回值 | 对应下一节点 | 结束或回接位置 |
| --- | --- | --- | --- |
| `draft_status == "calling_tools"` | `"tools"` | `tools` | 工具执行后固定边回 `draft` |
| `draft_status == "ready"` | `"check"` | `check` | 检查后由 `check_judge` 路由 |
| 其他（含 `"failed"`） | `"reply_failed"` | `reply_failed` | 回退用户输入后 `END` |

### R3. `commit_reply`

- 定位与签名：`commit_reply(state: AgentState)`，同步函数，[agent/node/draft.py:120](../../../../agent/node/draft.py#L120)。
- 调用方与条件：`check_judge` 返回 `"commit_reply"` 时由框架调度；服务模式下还被 `observed` 包装以触发落库回调。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `draft_reply` | `str` | `check` 通过前保留的候选 | 必填 | 提交正文 |
| `draft_status` | `str` | `draft` | 必须为 `"ready"` | 校验 |
| `check_status` | `str` | `check` | 必须为 `"passed"` | 校验 |
| `check_issues` | `list` | `check` | 必须为空 | 校验 |
| `check_reply` | `str` | `check` | 必须与 `draft_reply` 相同 | 校验“提交的正是被检查的文本” |
| `turn_id` | `str` | `begin_turn` | 必填 | 生成稳定消息 ID |
| `draft_reasoning` | `str` | `draft` | 可空 | 写入消息 `additional_kwargs` |
| `draft_usage` | `dict` 或 `None` | `draft` | 可空 | 写入消息 `usage_metadata` |

功能：任一校验不通过抛 `ValueError`（“禁止提交未通过自检或检查后发生变化的草稿”“缺少本轮 ID”）。通过后构造 `AIMessage(id=f"reply_{turn_id}", content=f"<timestamp>{get_time()}</timestamp>\n" + reply)`，有推理文本时附加 `additional_kwargs={"reasoning_content": ...}`，并携带 `usage_metadata`。

| 输出或状态字段 | 类型 | 产生条件 | 含义 | 消费方 |
| --- | --- | --- | --- | --- |
| `messages` | `[AIMessage]` | 校验通过 | 正式角色回复，ID 稳定可幂等 | 历史/SSE、`participant_state_out` |
| `draft_reply`/`draft_status`/`check_reply`/`draft_reasoning`/`draft_usage` | 清空 | 校验通过 | 候选状态收尾 | — |
| `reply_error` | `""` | 校验通过 | 清除失败标记 | — |

副作用：无数据库写入；服务模式的落库由 `on_commit` 回调完成（先落库再合并 checkpoint）。

异常与边界：状态不满足校验时抛错，运行被服务端标记失败并触发修复；稳定 ID 避免 checkpoint 重放产生重复回复。

后续去向：固定边到 `participant_state_out`。

### R4. `reply_failed`

- 定位与签名：`reply_failed(state: AgentState)`，同步函数，[agent/node/draft.py:138](../../../../agent/node/draft.py#L138)。
- 调用方与条件：`draft_judge`/`check_judge` 返回 `"reply_failed"` 时调度。

| 输入字段 | 类型 | 来源 | 用途 |
| --- | --- | --- | --- |
| `messages` | `list` | checkpoint | 查找最后一条 `HumanMessage` 及其后的失败轮消息 |
| `turn_id` | `str` | `begin_turn` | 日志标识 |

功能：反向查找最后一条 `HumanMessage` 下标；返回固定失败增量，并：
- 找到时，对其后的所有消息生成 `RemoveMessage`（必须显式删除，返回历史切片不会截断消息队列），设置 `retry_message_id` 为该用户消息 ID，`reply_error` 改为“已退回最近的用户输入，请重新提交该输入重试。”；无残留消息时只设置 ID；
- 未找到时保留通用失败文案，不修改消息。

| 输出或状态字段 | 类型 | 产生条件 | 含义 | 消费方 |
| --- | --- | --- | --- | --- |
| `messages` | `list[RemoveMessage]` | 找到用户输入且有后续消息 | 删除失败轮的 AI/工具消息 | `begin_turn` 下一轮去重 |
| `retry_message_id` | `str` | 找到用户输入 | 重试输入定位 | `begin_turn` |
| `reply_error` | `str` | 总是 | 失败原因，供服务端判定 `run.failed` 与前端提示 | `AgentAdapter.execute` |
| `draft_*`/`check_*` | 清空 | 总是 | 清理本轮候选 | — |

副作用：失败日志（含删除消息数）。异常与边界：不抛出异常；没有用户消息时只记录失败。

后续去向：固定边到 `END`；下一轮用户重试由 `begin_turn` 去重后重新走流程。

## 分支与异常链

- **工具循环**：`calling_tools` → `tools` → 固定边回 `draft`；工具消息原样保留在 `messages` 中，后续轮次仍可读取检索原文。
- **修订循环**：`check_status="failed"` 且轮数未耗尽时回到 `draft`，注入上一版草稿（拒答问题除外）与检查意见；内容修订上限 5 轮，与单次接口重试（draft 4 次、check 3 次）互相独立。
- **拒答**：`check` 的本地规则命中 `refusal` 时，修订不注入 `<previous_draft>`，直接要求重新生成。
- **模型接口失败**：4 次尝试后 `draft_status="failed"`，经 `draft_judge` 进入 `reply_failed`。
- **提交校验失败**：抛异常，运行失败；服务端 `repair` 不会重放已提交正文。

## 输入输出示例

适用 R1（正常生成，无工具调用）：

```text
输入：draft_status="pending"，messages=[HumanMessage("今天好累")]，check_feedback=""
模型响应：content="（我抬头看了看你。）\n那先坐会儿。"
输出：{"check_status":"pending","check_issues":[],"check_reply":"","draft_usage":{...},
       "draft_reasoning":"",
       "draft_reply":"（我抬头看了看你。）\n那先坐会儿。","draft_status":"ready"}
```

适用 R1（工具调用分支）：

```text
模型响应：tool_calls=[{name:"memory_query", id:"call_1", args:{...}}]
输出：{"check_status":"pending",..., "messages":[AIMessage(tool_calls=[...])],
       "draft_status":"calling_tools"}
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 检查：[../check/README.md](../check/README.md) · 工具：[../../tools/README.md](../../tools/README.md)
- 协议：[../../classes/state/README.md](../../classes/state/README.md) · 提示词：[../../prompts/main/draft/README.md](../../prompts/main/draft/README.md)
- 服务集成：[server/services/agent.py](../../../../server/services/agent.py)（`on_commit` 落库与稳定 ID 校验）
- 依据：`agent/node/draft.py`；`tests/test_rp_pipeline.py`、`tests/test_reply_memory.py` 覆盖候选生成、工具循环与失败回退；本次未执行测试。
