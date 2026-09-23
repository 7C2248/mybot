"""check 节点的结构化输出协议。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CheckIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["format", "empty_reply", "refusal", "style", "person"]
    reply_span: str
    suggested_fix: str = Field(min_length=1)


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["passed", "failed"]
    issues: list[CheckIssue]

    @model_validator(mode="after")
    def validate_verdict(self):
        if (self.verdict == "passed") != (len(self.issues) == 0):
            raise ValueError("通过时 issues 必须为空，失败时必须有具体问题")
        return self
