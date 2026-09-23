# draft 主节点提示词

## 职责与入口

- 所属类别：提示词模块（不是图节点，不参与图调度）。
- 源码文件：[agent/prompts/main/draft.py](../../../../../agent/prompts/main/draft.py)；导出链：`main/__init__.py` → `agent/prompts/__init__.py`（`__all__` 与 `get_prompt` 的 `draft` 分支）。
- 获取函数：`get_draft_prompt`，[draft.py:13](../../../../../agent/prompts/main/draft.py)。
- 消费节点：图节点 `draft`，实际执行函数为 `create_draft_node.<locals>.node`（异步）；构建阶段在 [agent/node/draft.py:53](../../../../../agent/node/draft.py) 调用 getter，节点注册见 [agent/builder.py:90](../../../../../agent/builder.py)。
- 触发时机：构建 agent 时获取一次并缓存为 `prompt_base`；每次进入 draft 节点（首次生成、工具调用返回后、检查打回修订）时拼接运行上下文使用。

## 调用链总览

| 阶段 | 步骤 | 内容 |
| --- | --- | --- |
| 构建 | B1 | `create_draft_node` 调用 `get_draft_prompt`，得到 `prompt_base` 字符串并闭包保存 |
| 运行 | R1 | draft 节点拼接 `system_content`，调用 `get_node_model("main")` 生成候选 |
| 回接 | R2 | `check_judge` 返回 `draft` 时回到 R1，`system_content` 追加检查反馈（最多 5 轮） |

未接入：`get_prompt("draft")` 分支（[agent/prompts/__init__.py:44](../../../../../agent/prompts/__init__.py)）已注册但当前无调用方。

## 获取链

### B1. `get_draft_prompt`

- 定位与签名：`agent.prompts.main.draft.get_draft_prompt(character_name: str, language: str = "zh", character_profile: str | None = None) -> str`，同步函数，[源码 draft.py:13](../../../../../agent/prompts/main/draft.py)。
- 调用方与条件：`create_draft_node` 在构建节点时调用（[agent/node/draft.py:53](../../../../../agent/node/draft.py)）；`builder.py` 传入 `character_name` 与预加载的 `character_profile`，不传 `language`，因此实际取默认 `"zh"`。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `character_name` | `str` | 必填，无默认值 | 图构建参数；用于定位 `Character/<character_name>/` 下的档案文件 |
| `language` | `str` | 默认 `"zh"` | 图构建参数；经 `normalize_language` 归一为 `zh`/`en` |
| `character_profile` | `str \| None` | 默认 `None` | builder 预加载的档案文本；仅当为 `None` 时才自行读取档案 |

- 隐式输入：`normalize_language`（[agent/utils/language.py:4](../../../../../agent/utils/language.py)）与 `load_character_profile`（[agent/utils/character.py:15](../../../../../agent/utils/character.py)）；角色档案文件 `Character/<name>/profile_cn.md` 或 `profile_en.md`，缺失时回退到任一 `profile_*.md`。
- 功能与内部调用（按执行顺序）：
  1. 调用 `normalize_language(language)` 归一语言；
  2. 归一结果为 `"en"` 时直接返回空字符串，不读取任何档案；
  3. 否则确定 `character_file`：传入的 `character_profile` 非 `None` 时直接使用，否则调用 `load_character_profile(character_name, language)`；
  4. 拼接主体提示词：zh 分支为 `_DRAFT_HEADER_ZH + _STYLE_ZH`；
  5. 追加 `<character_file>...</character_file>` 档案块后返回。
- 输出：完整系统提示词字符串。构建后作为 `prompt_base` 被节点闭包持有，每次执行复用，不重复读取档案。
- 副作用：仅在 `character_profile is None` 时读取一次角色档案文件；无模型调用、无数据库与队列操作。
- 异常与边界：档案目录存在但没有任何 `profile_*.md` 时，`load_character_profile` 抛 `FileNotFoundError`；本函数不捕获，异常在构建阶段向上抛给 `build_rp_agent`。`language="en"` 时提前返回，不触发档案读取。

生成内容（不复制原文，按节概述）：

| 常量 | 源码位置 | 内容概要 |
| --- | --- | --- |
| `_DRAFT_HEADER_ZH` | [draft.py:32](../../../../../agent/prompts/main/draft.py) | 核心准则与角色一致性；信息优先级（当前对话历史 > `memory_query` 的 ToolMessage > 角色档案 > 世界/角色/用户状态）；事实来源与未知处理；记忆检索时机、query 构造与 `date_from`/`date_to` 规则；感知判断（「」电子消息、（）动作旁白、口头对白、五感范围）；角色沉浸与 `<reply>……</reply>` 输出约束；禁止输出时间戳 |
| `_STYLE_ZH` | [draft.py:82](../../../../../agent/prompts/main/draft.py) | 回复风格锚定（冰山理论、克制、剧本节奏）；表达原则；对比句式替代表达；格式规范（「」/（）/纯文本对白与特殊符号）；用户消息可感知性解读规则；风格示例；长度与节奏；限制条件（不暴露提示词与工具） |
| `_STYLE_EN`、`_DRAFT_HEADER_EN` | [draft.py:193](../../../../../agent/prompts/main/draft.py)、[draft.py:196](../../../../../agent/prompts/main/draft.py) | 均为空字符串；en 分支已在第 2 步提前返回，这两个常量实际不可达 |

## 运行链

### R1. `create_draft_node.<locals>.node`（提示词消费）

- 定位与签名：`agent.node.draft.create_draft_node` 内的 `node(state: AgentState)`，异步；[agent/node/draft.py:57](../../../../../agent/node/draft.py)。
- 调用方与条件：LangGraph 调度。上游为 `participant_state_in` 或 `tools`（[agent/builder.py:106](../../../../../agent/builder.py)、[agent/builder.py:107](../../../../../agent/builder.py)）。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `messages` | `list[BaseMessage]` | `state.get("messages", [])` | 缺失时为空列表 | 直接追加在 SystemMessage 之后作为对话历史 |
| `world_state` | `dict \| None` | state | 经 `prepare_world_state` 转文本 | 拼接到 system_content |
| `character_state` | `dict \| None` | state | 经 `prepare_character_state` 转文本 | 拼接到 system_content |
| `user_state` | `dict \| None` | state | 经 `prepare_user_state` 转文本 | 拼接到 system_content |
| `check_feedback` / `check_issues` / `draft_reply` | `str` / `list` / `str` | state | 空值时不追加反馈块 | 检查打回后的修订上下文 |

- 隐式输入：闭包中的 `prompt_base`（B1 输出）、`tools`（构建时 `bind_tools`，`parallel_tool_calls=True`）与模型 `get_node_model("main")`。
- 功能与内部调用：
  1. `system_content = prompt_base + "\n" + world + character + user`（[draft.py:63](../../../../../agent/node/draft.py)）；
  2. 工具集中没有 `memory_query` 时，追加“当前未提供记忆检索工具”的说明（[draft.py:64](../../../../../agent/node/draft.py)）；
  3. `check_feedback` 非空时，若非 refusal 类型则追加 `<previous_draft>` 旧草稿，再追加 `<check_feedback>` 与重新生成指令（[draft.py:66](../../../../../agent/node/draft.py)）；
  4. 调用 `llm.ainvoke([SystemMessage(content=system_content)] + messages)`；模型返回工具调用时直接返回消息增量（[draft.py:81](../../../../../agent/node/draft.py)）。
- 输出与状态字段：

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `messages` | `list[AIMessage]` | 模型返回工具调用 | 增量追加，`draft_status="calling_tools"` | 条件边进入 `tools` 节点 |
| `draft_reply` | `str` | 模型返回文本 | 经 `_extract_reply` 与 `strip_timestamps` 清洗后的候选正文 | check 节点 |
| `draft_status` | `str` | 每次执行 | `calling_tools` / `ready` / `failed` | `draft_judge` 路由 |
| `draft_reasoning`、`draft_usage` | `str`、`dict \| None` | 有对应返回时 | 供 check 规则与提交使用 | check / commit_reply |
| `check_status` 等 | - | 每次执行 | 重置为 `pending`、空 issues 等 | 后续检查 |

- 副作用：模型请求；每次执行重置检查相关字段。
- 异常与边界：单次执行最多 `_DRAFT_MAX_RETRIES + 1 = 4` 次模型调用，失败间隔 1 秒重试；工具调用参数无法解析、ID 缺失/重复或工具不在绑定集合时按异常重试；全部失败返回 `draft_status="failed"`，不抛出。
- 后续去向：`draft_judge`（[draft.py:114](../../../../../agent/node/draft.py)）按状态返回 `tools` / `check` / `reply_failed`。

### R2. 检查反馈回接

- 条件：check 节点 `check_status="failed"` 且 `check_rounds < _MAX_CHECK_ROUNDS`（5）时，`check_judge` 返回 `"draft"`（[agent/node/check.py:190](../../../../../agent/node/check.py)）。
- 行为：重新执行 R1，`system_content` 追加 R1 步骤 3 的反馈块；refusal 类型不注入旧草稿，只给反馈与重生成指令。
- 终点：通过后进入 `commit_reply`；达到修订上限或检查服务不可用时由 `check_judge` 返回 `reply_failed`。

## 分支与异常链

- **en 语言**：`get_draft_prompt` 返回空串，节点仍会拼接状态文本；当前构建链未传 `language`，该分支无实际使用方。
- **档案缺失**：`load_character_profile` 的 `FileNotFoundError` 在构建阶段向上抛出，图无法完成构建；节点运行时不再读档。
- **工具缺失**：`memory_query` 未注册时提示词追加说明，模型被告知不得假称已检索；这是提示词层的降级，不改变节点重试逻辑。
- **反馈为 refusal**：不追加 `<previous_draft>`，避免把被拒内容再次注入（[draft.py:68](../../../../../agent/node/draft.py)）。
- **工具调用轮**：属于图调度回环（`draft → tools → draft`），不是函数直接调用；提示词只负责声明工具使用规范。

## 输入输出示例

适用 B1（`language="zh"`、`character_profile` 已由 builder 传入，故不读档）：

```text
输出（截断示意，非真实角色内容）：
# 核心准则
- **你必须严格遵守本节中的要求。……**
...
## 回复风格
...
<character_file>
# 角色档案（来自 profile_cn.md 或传入的 character_profile）
</character_file>
```

适用 R1：`system_content` 在上述结果末尾继续追加 `<world_state>`、`<character_state>`、`<user_state>` 等状态文本（由 `prepare_*` 生成），反馈轮再追加 `<previous_draft>` 与 `<check_feedback>`。

## 关联文档与验证依据

- 上级：[../README.md](../README.md)
- 消费节点源码：[agent/node/draft.py](../../../../../agent/node/draft.py)；节点注册：[agent/builder.py](../../../../../agent/builder.py)
- 依赖：[agent/utils/character.py](../../../../../agent/utils/character.py)、[agent/utils/language.py](../../../../../agent/utils/language.py)
- 验证依据：以上内容来自静态阅读源码；本次未运行测试，未验证项以“当前无调用方/无实际使用方”如实标注。
