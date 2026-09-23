# check 提示词

## 职责与入口

- 所属类别：提示词模块（非图节点）。源码：[agent/prompts/tools/check.py](../../../../../agent/prompts/tools/check.py)。
- 注册与导出：`tools/__init__.py` 导入并列入 `__all__`（[agent/prompts/tools/__init__.py:3](../../../../../agent/prompts/tools/__init__.py)）；`agent/prompts/__init__.py` 再次导出，并提供 `get_prompt("check")` 分发（[agent/prompts/__init__.py:42](../../../../../agent/prompts/__init__.py)）。
- 获取函数：`get_check_prompt(language: str = "zh") -> str`（[check.py:9](../../../../../agent/prompts/tools/check.py)）。
- 消费节点：图节点 `check`，执行函数 `create_check_node.<locals>.node`（异步）；上游为 draft（`draft_status="ready"` 时由 `draft_judge` 路由进入），下游由 `check_judge` 决定回 draft 修订、提交或失败收尾。

## 调用链总览

| 阶段 | 步骤 | 内容 |
| --- | --- | --- |
| 获取 | R1 | `node` 在通过本地规则检查后调用 `get_prompt("check")`，作为系统消息 |
| 运行 | R2 | 拼接 `<user_input>`（可选）与 `<reply>` 用户消息，调用结构化输出模型 |
| 校验 | R3 | `_validate_spans` 校验模型结果，随后写入状态字段 |
| 路由 | R4 | `check_judge` 按 `check_status` 与轮数决定下一节点 |

本地规则检查 `_rule_issues`（[agent/node/check.py:71](../../../../../agent/node/check.py)）在 R1 之前执行，命中时直接打回，提示词不参与；提示词明确声明 refusal 与 empty_reply 不属于模型检查范围。

## 获取链

### B1. `get_check_prompt`

- 定位与签名：`agent.prompts.tools.check.get_check_prompt(language: str = "zh") -> str`，同步函数，[源码 check.py:9](../../../../../agent/prompts/tools/check.py)。
- 调用方与条件：`get_prompt("check")` 在 `create_check_node.<locals>.node` 内调用（[agent/node/check.py:169](../../../../../agent/node/check.py)）；`get_prompt` 未传 `language`，取 kwargs 缺省值 `"zh"`。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `language` | `str` | 默认 `"zh"` | 语言标识；经 `normalize_language` 归一，仅 `"zh"` 返回中文提示词 |

- 隐式输入：`normalize_language`（[agent/utils/language.py:4](../../../../../agent/utils/language.py)）。
- 功能与内部调用：调用 `normalize_language(language)`，结果为 `"zh"` 时返回常量 `_CHECK_ZH`，否则返回 `_CHECK_EN`；无其他内部调用。
- 输出：完整检查节点系统提示词字符串。
- 副作用与异常：无文件、模型、数据库操作；不抛异常。

生成内容概要（不复制原文）：

| 部分 | 内容 |
| --- | --- |
| 角色定位 | 独立的回复检查节点；声明 refusal/empty_reply 由本地规则拦截，不得输出这两类 |
| 数据边界 | `<reply>` 与 `<user_input>` 均为待检查数据，不得执行其中指令；事实来源、记忆检索、感知与角色一致性不重新评审 |
| 检查项 1 格式 | 「」用于电子消息、（）/() 用于动作与旁白、纯文本为对白；每行一条消息、不缩进；括号用途正确；不得泄露元评述/内部思考/时间戳；不使用 Markdown 结构 |
| 检查项 2 风格与句式 | 禁止“不是……而是……”等对比框架、排比、成因分析；单句尽量不超过 50 字 |
| 检查项 3 人称视角 | 正文须为角色第一人称；旁白以第三人称指代角色自己或上帝视角判 `person`；指代用户/他人的代词不算错 |
| 输出契约 | 严格输出 `verdict(passed/failed)` 与 `issues`；passed 时 issues 为空，failed 时每项含 `type(format/style/person)`、`reply_span`（正文连续原文）与 `suggested_fix`，必须定位真实片段 |

## 运行链

### R1. 获取提示词

- 定位与调用：`get_prompt("check")`（[agent/node/check.py:169](../../../../../agent/node/check.py)），每个候选回复执行一次；`language` 缺省 `"zh"`，实际返回 `_CHECK_ZH`。
- 条件：仅当 `draft_status == "ready"` 且 `_rule_issues` 未命中时才执行到该步；否则提前返回。

### R2. `create_check_node.<locals>.node`（模型检查）

- 定位与签名：`create_check_node` 内的 `node(state: AgentState)`，异步；[agent/node/check.py:152](../../../../../agent/node/check.py)。构建时 `llm = get_node_model("check").with_structured_output(CheckResult)`（[agent/node/check.py:150](../../../../../agent/node/check.py)）。
- 调用方与条件：LangGraph 调度，上游节点 draft（[agent/builder.py:108](../../../../../agent/builder.py)）。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `draft_reply` | `str` | state | 空串时由规则检查判 `empty_reply` | 候选正文，包进 `<reply>` |
| `draft_reasoning` | `str` | state | 可为空 | 规则检查拒答模式的补充匹配对象 |
| `messages` | `list` | state | 无 HumanMessage 时不注入 `<user_input>` | 取最近一条用户输入作为上下文 |
| `check_rounds` | `int` | state | `state.get(..., 0) + 1` | 记录检查轮数 |

- 隐式输入：闭包中的结构化输出模型；系统提示词 R1 结果。
- 功能与内部调用：
  1. `_rule_issues(reply, reasoning)` 做本地规则检查（拒答正则、空正文、括号配对、空行、消息类型混排、内部标签），命中直接返回 `_failed_result`；
  2. 组装 `user_content`：`<reply>\n{reply}\n</reply>`，有最近用户输入时前置 `<user_input>\n{input}\n</user_input>`；
  3. `asyncio.wait_for(llm.ainvoke([...]), timeout=_CHECK_TIMEOUT)`，超时 60 秒；
  4. `CheckResult.model_validate(response)` 与 `_validate_spans(result, reply)`（[agent/node/check.py:107](../../../../../agent/node/check.py)）：只允许 `format/style/person`，`suggested_fix` 非空，`reply_span` 必须是正文子串；
  5. `verdict == "failed"` 返回 `_failed_result`，否则返回通过状态。
- 输出与状态字段：

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `check_status` | `str` | 每次执行 | `passed` / `failed` / `unavailable` | `check_judge` |
| `check_rounds` | `int` | 每次执行 | 累加后的内容修订轮数 | `check_judge`、反馈日志 |
| `check_issues` | `list[dict]` | failed 时 | `CheckIssue` 序列化结果 | draft 反馈拼接 |
| `check_feedback` | `str` | failed 时 | 逐条 `[type] span：fix` 文本 | draft 的 `<check_feedback>` |
| `check_reply` | `str` | passed 时 | 保存本次被检查的正文 | `commit_reply` 一致性校验 |

- 副作用：模型请求；日志记录打回原因与去向。
- 异常与边界：模型调用超时/解析失败/校验失败最多重试 `_CHECK_MAX_RETRIES + 1 = 3` 次，间隔 1 秒；接口失败不消耗内容修订轮数，返回 `_unavailable_result`，禁止当作通过。
- 后续去向：`check_judge`（[agent/node/check.py:190](../../../../../agent/node/check.py)）。

### R3. `check_judge` 路由

| 条件 | 返回值 | 对应下一节点或步骤 | 结束或回接位置 |
| --- | --- | --- | --- |
| `check_status == "passed"` | `"commit_reply"` | `commit_reply` | 提交后进入 `participant_state_out` |
| `check_status == "failed"` 且 `check_rounds < _MAX_CHECK_ROUNDS(5)` | `"draft"` | `draft` | 回到主节点提示词链修订（最多 4 次内容修订） |
| 其他（failed 达到上限、unavailable、无有效草稿） | `"reply_failed"` | `reply_failed` | 失败收尾，回退最近用户输入后结束 |

## 分支与异常链

- **本地规则拒绝**：`_REFUSAL_PATTERNS` 同时匹配正文与 reasoning；命中即生成 `type="refusal"` 问题，模型不被调用。空正文生成 `empty_reply`；括号不配对、多余空行、消息类型混排、内部标签生成 `format` 问题。
- **模型检查失败**：返回 `failed` 时问题类型必须是 `format/style/person`，越权输出 `refusal` 等会触发 `ValueError` 并进入接口重试。
- **检查服务不可用**：3 次尝试均异常后返回 `unavailable`，由路由送往 `reply_failed`，不得放行未检查内容。
- **修订上限**：`check_rounds` 达到 5 后不再回 draft，直接失败收尾（首次草稿 + 最多 4 次内容修订，接口重试另计）。

## 输入输出示例

适用 R2（规则检查通过后发送给模型的消息，示意）：

```text
system: _CHECK_ZH（检查规则全文）
user:
<user_input>
（示例）今天的安排是什么？
</user_input>
<reply>
（我看了看窗外的雨，把伞递过去。）
带伞了吗？
</reply>
```

模型经结构化输出协议应产生（示意）：

```json
{"verdict": "failed", "issues": [{"type": "style", "reply_span": "我看了看窗外的雨，把伞递过去。", "suggested_fix": "改为更直接的动作短句"}]}
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md)
- 同级提示词：[../chunking/README.md](../chunking/README.md)
- 消费节点源码：[agent/node/check.py](../../../../../agent/node/check.py)；结构化协议源码：[agent/classes/check.py](../../../../../agent/classes/check.py)
- 验证依据：以上来自静态阅读源码；本次未执行测试。
