"""独立检查候选回复的格式、风格、人称和空正文，不重新提取事实或检索记忆。
拒答由本地正则规则识别，不进入模型检查范围。"""

import asyncio
import re

from langchain_core.messages import HumanMessage

from agent.classes.check import CheckIssue, CheckResult
from agent.utils.models import get_node_model
from agent.classes.state import AgentState
from agent.utils.text import content_text, strip_timestamps
from agent.prompts import get_prompt
from utils.daily_logger import get_logger

__all__ = ['create_check_node', 'check_judge']

logger = get_logger("node.check")
_CHECK_TIMEOUT = 60
_CHECK_MAX_RETRIES = 2
_MAX_CHECK_ROUNDS = 5  # 首次草稿 + 最多四次内容修订；接口重试另计

# 本地规则识别的模型拒答模式：助手身份声明、拒绝生成回复等典型措辞。
# 刻意排除“我不能陪你去”“无法提供更多信息”等剧情内拒绝与自然回避。
_REFUSAL_PATTERNS = (
    re.compile(
        r"\b(?:i\s+)?(?:cannot|can't|am unable to|am not able to)\s+"
        r"(?:provide|generate|produce|create)\s+"
        r"(?:this|that|the|any|such|a|an)?\s*"
        r"(?:response|reply|answer|content|output|roleplay|role-play|assistance)\b",
        re.IGNORECASE),
    re.compile(r"\bas an?\s+(?:ai|artificial intelligence|language model)\b", re.IGNORECASE),
    re.compile(r"作为(?:一个|一名)?(?:AI|人工智能|语言模型|大模型)"),
    re.compile(r"(?:抱歉[，。！？,.!?]?\s*[^。！？\n]{0,12})?"
               r"(?:我|我们|助手)\s*(?:也|再|又)?\s*(?:没法|无法|不能)\s*"
               r"(?:再|继续)*?(?:提供|生成|撰写|写)\s*(?:这|该|本次|此)?(?:条|个|份)?\s*"
               r"(?:回复|回答|响应|正文|内容)?(?!\s*更多)"),
    re.compile(r"(?:没法|无法|不能)\s*(?:再|继续)\s*(?:提供|生成|撰写|写)(?!\s*更多)"),
)

_MODEL_ISSUE_TYPES = frozenset({"format", "style", "person"})

_GROUP_OPEN = {"（": "）", "(": ")", "「": "」"}
_GROUP_CLOSE = {v: k for k, v in _GROUP_OPEN.items()}
_GROUP_TYPE = {"（": "action", "(": "action", "「": "electronic"}


def _mixed_type_line(reply: str) -> str:
    """逐行检测三种消息类型是否混排：电子消息「」、动作旁白（）/()、对白纯文本
    各占一行，同一行出现两种及以上类型时返回该行原文，否则返回空字符串。"""
    for line in reply.splitlines():
        stack = []
        types = set()
        for char in line:
            if char in _GROUP_OPEN:
                if not stack:
                    types.add(_GROUP_TYPE[char])
                stack.append(char)
            elif char in _GROUP_CLOSE:
                if stack and _GROUP_OPEN[stack[-1]] == char:
                    stack.pop()
            elif char.isspace():
                continue
            elif not stack:
                types.add("dialogue")
        if len(types) > 1:
            return line
    return ""


def _rule_issues(reply: str, reasoning: str = "") -> list[CheckIssue]:
    for pattern in _REFUSAL_PATTERNS:
        if pattern.search(reply) or (reasoning and pattern.search(reasoning)):
            return [CheckIssue(type="refusal", reply_span="",
                               suggested_fix="重试")]
    if not reply.strip():
        return [CheckIssue(type="empty_reply", reply_span="",
                           suggested_fix="候选正文为空，请生成非空角色回复")]
    issues = []
    # 栈检查同时验证括号顺序、配对和嵌套。
    pairs, stack = {"）": "（", ")": "(", "」": "「"}, []
    malformed = False
    for char in reply:
        if char in pairs.values():
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                malformed = True
                break
    if malformed or stack:
        issues.append(CheckIssue(type="format", reply_span=reply,
                                 suggested_fix="修正括号的配对、顺序和嵌套"))
    if blank := re.search(r"\r?\n[ \t]*\r?\n", reply):
        start, end = max(0, blank.start() - 8), min(len(reply), blank.end() + 8)
        issues.append(CheckIssue(type="format", reply_span=reply[start:end],
                                 suggested_fix="行与行之间最多一个换行符，删除多余空行"))
    if line := _mixed_type_line(reply):
        issues.append(CheckIssue(type="format", reply_span=line,
                                 suggested_fix="不同消息类型须分行：电子消息「」、动作旁白（）/() 与对白纯文本不得混在同一行"))
    if re.search(r"</?(?:thinking_process|step\d+_\w+|monologue|reply|timestamp)\b|[【】]", reply):
        issues.append(CheckIssue(type="format", reply_span=reply,
                                 suggested_fix="正文只保留角色动作和对白，移除内部独白、思考、容器和时间戳标签"))
    
    return issues


def _validate_spans(result: CheckResult, reply: str):
    for issue in result.issues:
        if issue.type not in _MODEL_ISSUE_TYPES:
            raise ValueError("模型检查不得越权输出规则检查类型，如 refusal")
        if not issue.suggested_fix.strip():
            raise ValueError("自检必须给出修改建议")
        if not issue.reply_span.strip() or issue.reply_span not in reply:
            raise ValueError("自检必须定位真实回复片段")


def _failed_result(issues: list[CheckIssue], rounds: int, *, source: str, turn_id: str) -> dict:
    feedback = "\n".join(
        f"- [{issue.type}] {issue.reply_span}：{issue.suggested_fix}" if issue.reply_span.strip()
        else f"- [{issue.type}] {issue.suggested_fix}" for issue in issues)
    destination = ("draft 修订" if rounds < _MAX_CHECK_ROUNDS
                   else "reply_failed（修订次数达到上限，回退最近用户输入）")
    logger.warning(
        "check 打回 | turn_id=%s | 第 %d/%d 轮 | 来源=%s | 去向=%s | 原因=%s",
        turn_id, rounds, _MAX_CHECK_ROUNDS, source, destination, feedback,
    )
    return {"check_status": "failed", "check_rounds": rounds,
            "check_issues": [issue.model_dump() for issue in issues], "check_reply": "",
            "check_feedback": feedback}


def _unavailable_result(state: AgentState, feedback: str) -> dict:
    logger.warning(
        "check 打回 | turn_id=%s | 已完成检查轮数=%d | 去向=reply_failed | 原因=%s",
        state.get("turn_id", ""), state.get("check_rounds", 0), feedback,
    )
    return {"check_status": "unavailable", "check_reply": "", "check_issues": [],
            "check_feedback": feedback}


def _latest_user_input(messages: list) -> str:
    """提取最近一条用户输入，移除时间戳和首尾空白，供模型判断上下文。"""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return strip_timestamps(content_text(message.content)).strip()
    return ""


def create_check_node():
    llm = get_node_model("check").with_structured_output(CheckResult)

    async def node(state: AgentState):
        reply = (state.get("draft_reply") or "").strip()
        if state.get("draft_status") != "ready":
            return _unavailable_result(state, "没有有效候选回复，禁止放行")
        rounds = state.get("check_rounds", 0) + 1
        issues = _rule_issues(reply, state.get("draft_reasoning") or "")
        if issues:
            return _failed_result(issues, rounds, source="规则检查", turn_id=state.get("turn_id", ""))

        for attempt in range(_CHECK_MAX_RETRIES + 1):
            try:
                user_content = "<reply>\n" + reply + "\n</reply>"
                user_input = _latest_user_input(state.get("messages", []))
                if user_input:
                    user_content = "<user_input>\n" + user_input + "\n</user_input>\n" + user_content
                response = await asyncio.wait_for(
                    llm.ainvoke([
                        {"role": "system", "content": get_prompt("check")},
                        {"role": "user", "content": user_content},
                    ]), timeout=_CHECK_TIMEOUT)
                result = CheckResult.model_validate(response)
                _validate_spans(result, reply)
                if result.verdict == "failed":
                    return _failed_result(result.issues, rounds, source="模型检查",
                                          turn_id=state.get("turn_id", ""))
                logger.info("check 检查通过")
                return {"check_status": "passed", "check_rounds": rounds,
                        "check_issues": [], "check_feedback": "", "check_reply": reply}
            except Exception as exc:
                logger.warning(f"自检尝试 {attempt + 1} 失败: {type(exc).__name__}: {exc}")
                if attempt < _CHECK_MAX_RETRIES:
                    await asyncio.sleep(1.0)
        # 接口失败不消耗内容修订轮数，也不能视为检查通过。
        return _unavailable_result(state, "自检服务重试耗尽，未返回有效结果")

    return node


def check_judge(state: AgentState) -> str:
    if state.get("check_status") == "passed":
        return "commit_reply"
    if state.get("check_status") == "failed" and state.get("check_rounds", 0) < _MAX_CHECK_ROUNDS:
        return "draft"
    return "reply_failed"
