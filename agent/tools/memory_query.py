# agent/tools/memory_query.py
# 角色长期记忆检索工具：供 agent 节点以工具调用方式自循环查询
#
# 与原"单次前置注入"流程的区别:
#   - 由主 LLM 自主决定何时查询、查询什么、查询几轮（agent -> tools -> agent 自循环）
#   - 每次调用执行一条日志体关键词查询的混合检索（向量 + 关键词 + 时间过滤）
#   - 结果以 ToolMessage 形式进入对话上下文，不再注入 system prompt

from typing import Any, Optional

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from agent.utils.models import get_qwen_embedding_model
from agent.classes.memory import MemoryHit, MemoryQueryInput, MemorySearchResult
from utils.daily_logger import get_logger

logger = get_logger("memory.query")

_DEFAULT_FINAL_LIMIT = 12


class CharacterMemoryQueryTool(BaseTool):
    """角色长期记忆检索工具（依赖 memory_store，由工厂函数创建）。"""

    name: str = "memory_query"
    description: str = (
        "查询角色的长期记忆库（混合检索：语义向量 + 关键词 + 时间过滤），"
        "记忆库包含当前对话窗口之外的长期记录。\n"
        "何时调用：回应需要、而角色在当前对话中尚未亲口说出细节的记忆——双方习惯/约定、"
        "未兑现承诺、共同经历、特定人事物的提及等，无需用户明确提问；"
        "或者初次在对话记录中出现某个事物，如称呼、非日常的某些物品以及关系经历等时，需要先通过记忆检索确认是否真的为首次出现。"
        "对话历史中没提到 ≠ 记忆库中不存在，无法区分首次提及与被遗忘的旧约定时，先查询再判断。\n"
        "查询构造：query 用日志体关键词序列（时间锚点+地点+核心实体+事件关键词），"
        "空格分隔无连接词，不超过30字符，每次调用只聚焦一个检索维度；"
        "代词解析为具体实体，相对时间换算为绝对日期（阿拉伯数字）；"
        "date_from/date_to 按 yyyy-mm-dd 成对提供，仅用于有时间依据的特定时间段事件查询，"
        "模糊给整月/整季，明确给当天及前后1~2天；偏好/承诺内容/属性类非时间特定查询省略。"
        "日期区间不得倒置，未知时间不强加筛选条件；query 中仍保留已知时间锚点。\n"
        "支持沿记忆中的日期、约定、事件引用连续追索，不限制为一次检索。"
        "返回结构化 ok/empty/error 与父记忆 ID；相关命中不代表事实已得到支持。"
        "证据足够或本批检索未增加新的父记忆/内容版本时停止。"
        "空结果和检索错误不能证明事件从未发生。"
    )
    args_schema: type[BaseModel] = MemoryQueryInput

    # AsyncPostgresCharacterMemoryStore 实例（任意类型，避免 pydantic schema 生成）
    memory_store: Any = None

    def _run(self, *args, **kwargs) -> str:
        raise NotImplementedError("memory_query 仅支持异步调用")

    async def _arun(self, query: str, date_from: Optional[str] = None,
                    date_to: Optional[str] = None,
                    limit: int = _DEFAULT_FINAL_LIMIT) -> str:
        try:
            if self.memory_store is None:
                raise RuntimeError("memory store unavailable")
            params = MemoryQueryInput(
                query=query, date_from=date_from, date_to=date_to, limit=limit,
            ).model_dump(mode="json")
            encoder = get_qwen_embedding_model()
            embedding = encoder.encode(params["query"], prompt_name="query")
            rows = await self.memory_store.search_hybrid(
                embedding, query_text=params["query"], keyword_text=params["query"],
                date_from=params["date_from"], date_to=params["date_to"],
                final_limit=params["limit"],
            )
            hits = {}
            for row in rows:
                hit = MemoryHit(
                    memory_id=row["id"], text=row["memory"],
                    event_date=str(row["event_date"]) if row.get("event_date") else None,
                    update_time=str(row["update_time"]) if row.get("update_time") else None,
                )
                hits[hit.memory_id] = hit
            logger.info(f"query={params['query']!r}, date_from={params['date_from']}, "
                        f"date_to={params['date_to']}, parent_hits={len(hits)}")
            return MemorySearchResult(
                status="ok" if hits else "empty", hits=list(hits.values()),
            ).model_dump_json()
        except Exception as exc:
            logger.warning(f"记忆检索失败: {type(exc).__name__}")
            return MemorySearchResult(
                status="error", hits=[], error=type(exc).__name__,
            ).model_dump_json()


def create_memory_query_tool(memory_store) -> CharacterMemoryQueryTool:
    """工厂函数：绑定 memory_store 创建记忆检索工具实例。"""
    return CharacterMemoryQueryTool(memory_store=memory_store)
