# check 节点（候选检查与检查后路由）

## 职责与入口

- 所属类别：图节点工厂 + 路由函数。
- 源码：[agent/node/check.py](../../../../agent/node/check.py)
- 图注册名：`check` → `create_check_node()` 返回的 `node`；`check_judge` 为 `check` 的条件路由（[agent/builder.py](../../../../agent/builder.py)）。
- 上游/下游：`draft_judge` 返回 `"check"` 时进入；结果经 `check_judge` 去 `commit_reply`、`draft` 或 `reply_failed`。
- 检查范围：格式、风格、人称、空正文；**拒答由本地正则规则识别，不交给模型**。

## 调用链总览

| 阶段 | 步骤 | 关系 |
| --- | --- | --- |
| 构建 | B1 `create_check_node()` | 工厂：模型 + 结构化输出 |
| 运行 | R1 `node(state)` | 图调度 |
| 运行 | R1.1 `_rule_issues(reply, reasoning)` | 本地规则检查（含 `_mixed_type_line`） |
| 运行 | R1.2 `_latest_user_input(messages)` | 提取最近用户输入 |
| 运行 | R1.3 `llm.ainvoke(...)` + `CheckResult.model_validate` | 异步模型检查（超时/重试） |
| 运行 | R1.4 `_validate_spans(result, reply)` | 校验模型输出边界 |
| 运行 | R1.5 `_failed_result` / `_unavailable_result` | 结果构造 |
| 运行 | R2 `check_judge(state)` | 条件路由 |

## 构建链

### B1. `create_check_node`

- 定位与签名：`create_check_node()`，同步工厂，返回 `async def node(state)`，[agent/node/check.py:149](../../../../agent/node/check.py#L149)。
- 调用方：`build_rp_agent` 注册 `check` 时调用。

功能：`llm = get_node_model("check").with_structured_output(CheckResult)`，把模型输出约束为 [CheckResult](../../classes/check/README.md)；闭包捕获 `llm`。

输出：执行函数 `node`。副作用：构建期读取节点模型配置。

## 运行链

### R1. `node(state)`

- 定位与签名：`create_check_node.<locals>.node(state: AgentState)`，异步函数，[agent/node/check.py:152](../../../../agent/node/check.py#L152)。
- 调用方与条件：`draft_judge` 返回 `"check"` 时由框架调度。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `draft_reply` | `str` | `draft` | 缺省 `""` | 待检查正文 |
| `draft_status` | `str` | `draft` | 必须为 `"ready"` 才继续 | 防止空/失败候选被放行 |
| `draft_reasoning` | `str` | `draft` | 缺省 `""` | 拒答规则同时检查推理文本 |
| `check_rounds` | `int` | checkpoint | 缺省 `0` | 本次是第几轮内容检查 |
| `messages` | `list` | checkpoint | 缺省 `[]` | 提取最近用户输入作为模型上下文 |
| `turn_id` | `str` | `begin_turn` | 可空 | 日志标识 |

隐式输入：闭包中的结构化输出模型；`get_prompt("check")`（[../../prompts/tools/check/README.md](../../prompts/tools/check/README.md)）；常量 `_CHECK_TIMEOUT=60`、`_CHECK_MAX_RETRIES=2`、`_MAX_CHECK_ROUNDS=5`、拒答正则 `_REFUSAL_PATTERNS`。

功能与内部调用：

1. 取 `reply = (draft_reply or "").strip()`；若 `draft_status != "ready"` 返回 `_unavailable_result(state, "没有有效候选回复，禁止放行")`。
2. `rounds = check_rounds + 1`。
3. `issues = _rule_issues(reply, draft_reasoning)`（R1.1）；有规则问题直接返回 `_failed_result(issues, rounds, source="规则检查", ...)`（R1.5），**不调用模型**。
4. 最多 3 次模型检查（`_CHECK_MAX_RETRIES + 1`）：
   - 组装 `<reply>` 容器；有最近用户输入时前置 `<user_input>`（R1.2）；
   - `asyncio.wait_for(llm.ainvoke([...]), timeout=60)`（R1.3）；
   - `CheckResult.model_validate(response)` 与 `_validate_spans(result, reply)`（R1.4）；
   - `verdict == "failed"` → `_failed_result(result.issues, rounds, source="模型检查", ...)`；
   - `verdict == "passed"` → 返回 `{"check_status": "passed", "check_rounds": rounds, "check_issues": [], "check_feedback": "", "check_reply": reply}`。
   - 超时/解析失败记录 warning，间隔 1 秒重试。
5. 重试耗尽：`_unavailable_result(state, "自检服务重试耗尽，未返回有效结果")`；接口失败**不消耗内容修订轮数**，也绝不视为通过。

### R1.1. `_rule_issues`

- 定位与签名：`_rule_issues(reply: str, reasoning: str = "") -> list[CheckIssue]`，同步私有函数，[agent/node/check.py:71](../../../../agent/node/check.py#L71)。
- 检查顺序与规则：
  1. **拒答**：`reply` 或 `reasoning` 命中 `_REFUSAL_PATTERNS`（AI 身份声明、“无法生成回复”类措辞等 5 组正则）→ 返回单项 `CheckIssue(type="refusal", reply_span="", suggested_fix="重试")`。
  2. **空正文**：`reply.strip()` 为空 → `type="empty_reply"`。
  3. **括号配对**：用栈检查 `（）`、`()`、`「」` 的配对、顺序和嵌套 → `type="format"`，`reply_span` 为整段回复。
  4. **空行**：命中 `\r?\n[ \t]*\r?\n` → `type="format"`，span 截取空行前后 8 字符。
  5. **消息类型混排**：`_mixed_type_line` 返回非空行 → `type="format"`，span 为该行。
  6. **内部标签**：命中 `</?(thinking_process|step\d+_\w+|monologue|reply|timestamp)\b` 或 `【】` → `type="format"`。
- 输出：`CheckIssue` 列表；空列表表示规则检查通过。

### R1.2. `_latest_user_input`

- 定位与签名：`_latest_user_input(messages: list) -> str`，同步私有函数，[agent/node/check.py:141](../../../../agent/node/check.py#L141)。
- 行为：反向查找第一条 `HumanMessage`，`strip_timestamps(content_text(...)).strip()` 后返回；找不到返回 `""`。
- 用途：作为 `<user_input>` 供模型判断格式/风格/人称；提示词明确它同样不得被执行。

### R1.3. 模型检查

- 输入：system 为 `check` 提示词，user 为 `<user_input>`（可选）+ `<reply>`。
- 输出：经 `with_structured_output` 校验的 `CheckResult`；模型只能输出 `format`/`style`/`person` 三类问题。
- 异常：超时、JSON/schema 校验失败、`_validate_spans` 抛错都按失败重试。

### R1.4. `_validate_spans`

- 定位与签名：`_validate_spans(result: CheckResult, reply: str)`，同步私有函数，[agent/node/check.py:107](../../../../agent/node/check.py#L107)。
- 校验：问题类型必须属于 `_MODEL_ISSUE_TYPES={"format","style","person"}`（模型不得输出 `refusal`/`empty_reply`）；`suggested_fix` 非空；`reply_span` 非空且必须是 `reply` 的真实子串。违反则抛 `ValueError`，触发重试。

### R1.5. `_failed_result` / `_unavailable_result`

- `_failed_result(issues, rounds, *, source, turn_id)`：[agent/node/check.py:117](../../../../agent/node/check.py#L117)。把问题拼成多行 feedback（`- [type] span：fix`，无 span 时省略 span）；按 `rounds < _MAX_CHECK_ROUNDS` 记录去向（`draft 修订` 或 `reply_failed`）；返回 `check_status="failed"`、`check_rounds=rounds`、`check_issues`（`model_dump` 列表）、`check_reply=""`、`check_feedback=feedback`。
- `_unavailable_result(state, feedback)`：[agent/node/check.py:132](../../../../agent/node/check.py#L132)。返回 `check_status="unavailable"`、`check_reply=""`、`check_issues=[]`、`check_feedback=feedback`；不增加 `check_rounds`。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `check_status` | `str` | 总是 | `passed`/`failed`/`unavailable` | `check_judge`、`commit_reply` |
| `check_rounds` | `int` | 规则/模型检查后 | 已消耗的内容检查轮数 | `check_judge` |
| `check_issues` | `list[dict]` | 失败时 | 结构化问题，供 `draft` 判断拒答与修订 | `draft` |
| `check_feedback` | `str` | 失败/不可用时 | 注入下一版 `draft` 的修订意见 | `draft` |
| `check_reply` | `str` | 通过时 | 实际通过检查的文本，`commit_reply` 要求与 `draft_reply` 相同 | `commit_reply` |

副作用：规则失败/模型失败/通过均写日志；模型检查最多 3 次请求。

异常与边界：所有模型异常在节点内捕获并重试；规则检查不抛异常。`check_rounds` 在规则失败时同样增加，规则失败也消耗修订轮数。

后续去向：`check_judge`（R2）。

### R2. `check_judge`

- 定位与签名：`check_judge(state: AgentState) -> str`，同步路由，[agent/node/check.py:190](../../../../agent/node/check.py#L190)。

| 条件 | 返回值 | 对应下一节点 | 结束或回接位置 |
| --- | --- | --- | --- |
| `check_status == "passed"` | `"commit_reply"` | `commit_reply` | 提交后到 `participant_state_out` |
| `check_status == "failed"` 且 `check_rounds < 5` | `"draft"` | `draft` | 修订循环回接点 |
| 其他（`unavailable` 或轮数耗尽） | `"reply_failed"` | `reply_failed` | 回退用户输入后 `END` |

## 分支与异常链

- **规则失败**：不调用模型，`check_rounds` 加一；轮数达到 5 后 `check_judge` 返回 `reply_failed`。
- **模型判定失败**：与规则失败同样进入修订循环。
- **接口不可用**：`unavailable` 不消耗修订轮数，但 `check_judge` 仍返回 `reply_failed`，本轮不提交。
- **拒答**：本地规则返回 `type="refusal"`，`draft` 修订时不注入上一版草稿，避免模型沿用拒答。
- **内容修订与接口重试的区别**：`check_rounds` 记录内容修订轮数（上限 5）；单节点接口重试（draft 4 次、check 3 次）不增加 `check_rounds`。

## 输入输出示例

适用 R1（规则失败，括号不配对）：

```text
输入：draft_reply="（我笑了笑，然后说）\n别担心。", check_rounds=0
输出：{"check_status":"failed","check_rounds":1,
       "check_issues":[{"type":"format","reply_span":"...","suggested_fix":"修正括号的配对、顺序和嵌套"}],
       "check_reply":"","check_feedback":"- [format] ...：修正括号的配对、顺序和嵌套"}
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 提交：[../draft/README.md](../draft/README.md) · 协议：[../../classes/check/README.md](../../classes/check/README.md)
- 提示词：[../../prompts/tools/check/README.md](../../prompts/tools/check/README.md)
- 依据：`agent/node/check.py`；`tests/test_rp_pipeline.py`、`tests/test_event_judge.py` 等覆盖检查与路由；本次未执行测试。
