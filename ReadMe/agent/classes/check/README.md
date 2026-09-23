# CheckIssue / CheckResult（check 节点结构化输出协议）

## 职责与入口

- 所属类别：节点结构化输出协议（`agent/classes/check.py`），不是图节点，没有工厂、路由或可执行入口。
- 源码：[`check.py`](../../../../agent/classes/check.py)
- 生产与消费集中在 [`agent/node/check.py`](../../../../agent/node/check.py) 的 `create_check_node` 闭包内；`CheckResult` 同时作为 `with_structured_output` 的目标 schema 交给 check 模型。
- 产物最终以 `dict` 形式进入 `AgentState` 的 `check_issues`（`CheckIssue.model_dump()` 列表），供 draft 修订与 commit 校验使用。

## 定义

### `CheckIssue`

```python
class CheckIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["format", "empty_reply", "refusal", "style", "person"]
    reply_span: str
    suggested_fix: str = Field(min_length=1)
```

| 字段 | 类型 | 约束 | 语义 |
| --- | --- | --- | --- |
| `type` | `Literal["format","empty_reply","refusal","style","person"]` | 必填 | 问题分类：`format` 格式、`empty_reply` 空正文、`refusal` 模型拒答、`style` 风格、`person` 人称 |
| `reply_span` | `str` | 必填，可为空串 | 问题在候选正文中的定位片段；规则检查的 `refusal` / `empty_reply` 使用空串 |
| `suggested_fix` | `str` | 必填，`min_length=1` | 修改建议；`refusal` 固定为 `"重试"` |

`extra="forbid"` 拒绝模型输出未声明字段。

### `CheckResult`

```python
class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["passed", "failed"]
    issues: list[CheckIssue]

    @model_validator(mode="after")
    def validate_verdict(self):
        if (self.verdict == "passed") != (len(self.issues) == 0):
            raise ValueError("通过时 issues 必须为空，失败时必须有具体问题")
        return self
```

| 字段 | 类型 | 约束 | 语义 |
| --- | --- | --- | --- |
| `verdict` | `Literal["passed","failed"]` | 必填 | 检查结论 |
| `issues` | `list[CheckIssue]` | 必填，无默认值 | 失败原因列表；`passed` 时必须为空，`failed` 时必须非空 |

校验器保证结论与问题列表严格一致，因此消费方不需要处理“通过但带问题”或“失败但无原因”的畸形结果。

## 构造或校验

两条生产路径的校验强度不同：

1. **本地规则路径**：`_rule_issues(reply, reasoning)` 直接构造 `CheckIssue`，不经过模型与 `CheckResult`，因此允许 `reply_span=""` 且 `type` 可以是 `refusal` / `empty_reply`。
2. **模型路径**：
   - `create_check_node` 以 `get_node_model("check").with_structured_output(CheckResult)` 约束输出，随后 `CheckResult.model_validate(response)` 再次校验；
   - `_validate_spans(result, reply)` 追加三条业务校验，任一失败抛出 `ValueError` 并进入接口重试：
     - `issue.type` 必须属于 `_MODEL_ISSUE_TYPES = {"format","style","person"}`，否则报“模型检查不得越权输出规则检查类型”；
     - `suggested_fix` 去空白后不得为空；
     - `reply_span` 去空白后不得为空，且必须是候选正文的真实子串。
   - `verdict == "failed"` 时 `issues` 非空由 `CheckResult` 校验器保证。

## 生产方

| 生产位置 | 产物 | 触发条件与内容 |
| --- | --- | --- |
| `_rule_issues`（[`agent/node/check.py`](../../../../agent/node/check.py)） | `list[CheckIssue]` | 按顺序检查：① 拒答正则命中候选正文或思维链 → `type="refusal"`、`reply_span=""`、`suggested_fix="重试"`，立即返回；② 正文去空白后为空 → `empty_reply`，建议“候选正文为空，请生成非空角色回复”；③ 括号顺序/配对/嵌套错误、行间空行、同行混排消息类型、内部标签或 `【】` → `format`，各带具体建议 |
| check 模型 + `CheckResult.model_validate` | `CheckResult` | 规则检查全部通过后调用；模型只允许输出 `format` / `style` / `person` 问题，且必须给出可定位片段 |
| `_failed_result` | `AgentState["check_issues"]`、`check_status="failed"`、`check_rounds`、`check_feedback` | 规则或模型任一判定失败；`check_feedback` 逐行拼接为 `- [type] span：fix`（无 span 时省略 `span：`） |
| `_unavailable_result` | `check_status="unavailable"`、`check_issues=[]` | draft 非 `ready`，或模型接口重试耗尽；不消耗内容修订轮数，也不得视为通过 |

## 消费方

| 消费位置 | 读取内容 | 用途 |
| --- | --- | --- |
| `_validate_spans` | `CheckResult.issues` 的 `type`、`reply_span`、`suggested_fix` | 拒绝越权类型与不可定位片段 |
| `_failed_result` | `CheckIssue.model_dump()` | 序列化为状态字段；同一列表用于生成 `check_feedback` 文本 |
| draft 节点 | `state["check_issues"]` 中是否存在 `type == "refusal"` | 决定修订 prompt 是否携带 `<previous_draft>`（拒答文本不回流） |
| `commit_reply` | `state["check_issues"]` 必须为空、`check_status == "passed"` | 防止提交未通过检查的草稿 |
| `check_judge` | 不直接读取本协议，只读 `check_status` / `check_rounds` | 路由到 `draft` / `commit_reply` / `reply_failed` |
| 测试 | `CheckResult.model_fields`、结构化输出替身 | `tests/test_rp_pipeline.py` 验证字段集合、越权类型拒绝与空片段拒绝 |

## 输入输出示例

规则路径命中拒答（`draft_reply` 或 `draft_reasoning` 匹配 `_REFUSAL_PATTERNS`）后写入状态的增量：

```python
{
    "check_status": "failed",
    "check_rounds": 1,
    "check_issues": [{"type": "refusal", "reply_span": "", "suggested_fix": "重试"}],
    "check_reply": "",
    "check_feedback": "- [refusal] 重试",
}
```

模型路径判定失败时的结构化输出（经 `_validate_spans` 校验后写入）：

```json
{
  "verdict": "failed",
  "issues": [
    {"type": "person", "reply_span": "她看着你", "suggested_fix": "改为第一人称或角色自称"}
  ]
}
```

通过时：`CheckResult(verdict="passed", issues=[])` → 状态增量 `check_status="passed"`、`check_issues=[]`、`check_reply=reply`、`check_feedback=""`。

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)
- 兄弟协议：[`../state/README.md`](../state/README.md)、[`../participant_state/README.md`](../participant_state/README.md)
- 生产/消费节点：[`../../node/check/README.md`](../../node/check/README.md)、[`../../node/draft/README.md`](../../node/draft/README.md)
- 实现依据：[`agent/node/check.py`](../../../../agent/node/check.py)
- 测试覆盖（静态阅读交叉核对，未在本页重新执行）：`tests/test_rp_pipeline.py` 的 `test_check_only_receives_candidate_and_restricts_issue_types`、`test_check_api_errors_invalid_spans_and_timeout_never_pass`、`test_check_rejects_empty_malformed_and_leaked_internal_content` 等用例
- 未验证项：本协议不校验 `reply_span` 与 `suggested_fix` 的语言风格，相关约束由 Prompt 与模型负责。
