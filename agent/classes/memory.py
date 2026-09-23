"""记忆检索工具的输入和返回协议；结果仅包含父记忆原文。"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemorySearchQuery(_StrictModel):
    query: str = Field(
        min_length=1,
        description="日志体关键词检索序列：`<时间锚点> <地点> <核心实体> <事件/事实关键词>`；"
                    "空格分隔无连接词，不超过30字符，每次调用只聚焦一个检索维度。"
                    "代词解析为具体实体，角色自身用我表示；去除主观评价和叙事性措辞。"
                    "相对时间依据明确的事件/记忆时间锚点换算为绝对日期，日期数字使用阿拉伯数字，"
                    "query 中日期写作2026年11月20日；未知时间、地点等要素省略，不为凑结构编造。"
                    "提供日期区间时，query 中仍保留已知时间锚点。允许沿记忆中的引用继续追索。")
    date_from: date | None = Field(
        default=None,
        description="起始日期 yyyy-mm-dd；仅当查询指向有明确时间依据的特定时间段事件时提供"
                    "（模糊给整月/整季，明确给当天及前后1~2天），偏好/承诺内容/属性类非时间特定查询省略。"
                    "与 date_to 成对使用，不用未知日期或推测答案构造筛选条件。")
    date_to: date | None = Field(
        default=None,
        description="结束日期 yyyy-mm-dd；与 date_from 成对使用，结束日期不得早于起始日期；"
                    "其余情况省略。")

    @model_validator(mode="before")
    @classmethod
    def normalize_bare_query(cls, data):
        # 兼容模型省略日期、把 query 直接输出为字符串的退化形式。
        if isinstance(data, str):
            return {"query": data}
        return data

    @model_validator(mode="after")
    def validate_query(self):
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("query 不能为空")
        if (self.date_from is None) != (self.date_to is None):
            raise ValueError("日期范围必须成对提供")
        if self.date_from and self.date_from > self.date_to:
            raise ValueError("起始日期不得晚于结束日期")
        return self


class MemoryHit(_StrictModel):
    memory_id: int = Field(strict=True, gt=0, description="父记忆表主键")
    text: str = Field(min_length=1)
    event_date: str | None = None
    update_time: str | None = None


class MemorySearchResult(_StrictModel):
    status: Literal["ok", "empty", "error"]
    hits: list[MemoryHit]
    error: str | None = None

    @model_validator(mode="after")
    def validate_status(self):
        if self.status == "ok" and not self.hits:
            raise ValueError("ok 必须包含命中")
        if self.status != "ok" and self.hits:
            raise ValueError("empty/error 不得包含命中")
        if self.status == "error" and not self.error:
            raise ValueError("error 必须包含错误类型")
        if self.status != "error" and self.error is not None:
            raise ValueError("成功或空结果不得包含错误")
        if len({hit.memory_id for hit in self.hits}) != len(self.hits):
            raise ValueError("父记忆 ID 重复")
        return self


class MemoryQueryInput(MemorySearchQuery):
    """继承日志体关键词、时间区间要求和日期校验，供所有检索入口共用。"""

    limit: int = Field(
        default=12, strict=True, ge=1, le=20,
        description="返回记忆条数上限，一般保持默认")
