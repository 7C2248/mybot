# agent/tools/file_system.py
# 工作区文件工具：glob 检索、文本文件读写
#
# - 所有输入路径都解析并限制在工作区根目录内，禁止越界访问
# - 仅支持 UTF-8 文本文件；写入自动创建父目录并覆盖同名文件
# - 结果以结构化 JSON 字符串返回（status: ok/error），异常不外泄

from glob import glob
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from utils.daily_logger import get_logger

logger = get_logger("tools.file_system")

_DEFAULT_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _FileSearchInput(_StrictModel):
    pattern: str = Field(
        min_length=1,
        description="glob 匹配模式，相对于工作区根目录；支持 * ? [] 与 ** 递归匹配，"
                    "例如 scripts/**/*.py 或 **/*.md。")
    limit: int = Field(
        default=200, strict=True, ge=1, le=1000,
        description="返回匹配路径的最大条数，命中过多时调小")


class _FileReadInput(_StrictModel):
    path: str = Field(
        min_length=1,
        description="相对工作区根目录的文本文件路径")
    offset: int = Field(
        default=0, ge=0,
        description="起始行号（从 0 开始），用于分段读取长文件")
    limit: int = Field(
        default=2000, strict=True, ge=1, le=10000,
        description="读取的最大行数")


class _FileWriteInput(_StrictModel):
    path: str = Field(
        min_length=1,
        description="相对工作区根目录的目标文件路径，已存在将被覆盖")
    content: str = Field(
        min_length=1,
        description="写入的完整文本内容")


class _FileSearchResult(_StrictModel):
    status: Literal["ok", "error"]
    matches: list[str] = Field(default_factory=list)
    error: str | None = None


class _FileReadResult(_StrictModel):
    status: Literal["ok", "error"]
    path: str | None = None
    content: str = ""
    total_lines: int = 0
    error: str | None = None


class _FileWriteResult(_StrictModel):
    status: Literal["ok", "error"]
    path: str | None = None
    chars_written: int = 0
    error: str | None = None


def _resolve_path(workspace_root, path: str) -> Path | None:
    """把输入路径解析到工作区根目录内；越界或等于根目录时返回 None。"""
    root = Path(workspace_root or _DEFAULT_WORKSPACE_ROOT).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        return None
    return resolved


class FileSearchTool(BaseTool):
    """工作区 glob 文件检索工具（由工厂函数创建，路径限制在工作区内）。"""

    name: str = "search_files"
    description: str = (
        "在工作区中按 glob 模式查找文件路径，返回相对工作区根目录的路径列表。\n"
        "何时调用：需要定位项目文件、确认某类文件是否存在或浏览目录结构时；"
        "支持 * ? [] 与 ** 递归匹配，一次只提交一个模式，命中过多时用 limit 截断。"
    )
    args_schema: type[BaseModel] = _FileSearchInput

    # 工作区根目录（Path 或 None，任意类型避免 pydantic schema 生成）
    workspace_root: Any = None

    def _run(self, *args, **kwargs) -> str:
        raise NotImplementedError("search_files 仅支持异步调用")

    async def _arun(self, pattern: str, limit: int = 200) -> str:
        try:
            root = Path(self.workspace_root or _DEFAULT_WORKSPACE_ROOT).resolve()
            matches = []
            for match in sorted(glob(str(root / pattern), recursive=True)):
                resolved = Path(match).resolve()
                if resolved == root or not resolved.is_relative_to(root):
                    continue
                matches.append(resolved.relative_to(root).as_posix())
                if len(matches) >= limit:
                    break
            logger.info(f"pattern={pattern!r}, matches={len(matches)}")
            return _FileSearchResult(status="ok", matches=matches).model_dump_json()
        except Exception as exc:
            logger.warning(f"文件检索失败: {type(exc).__name__}")
            return _FileSearchResult(status="error", error=type(exc).__name__).model_dump_json()


class FileReadTool(BaseTool):
    """工作区文本文件读取工具（由工厂函数创建，路径限制在工作区内）。"""

    name: str = "read_file"
    description: str = (
        "读取工作区内一个 UTF-8 文本文件的内容，路径相对工作区根目录。\n"
        "何时调用：需要查看源码、配置、日志或文档内容时；"
        "长文件用 offset/limit 分段读取。不支持二进制文件。"
    )
    args_schema: type[BaseModel] = _FileReadInput

    # 工作区根目录（Path 或 None，任意类型避免 pydantic schema 生成）
    workspace_root: Any = None

    def _run(self, *args, **kwargs) -> str:
        raise NotImplementedError("read_file 仅支持异步调用")

    async def _arun(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        try:
            resolved = _resolve_path(self.workspace_root, path)
            if resolved is None:
                return _FileReadResult(
                    status="error", error="路径越出工作区根目录").model_dump_json()
            if not resolved.is_file():
                return _FileReadResult(
                    status="error", error="目标不存在或不是文件").model_dump_json()
            lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
            chunk = lines[offset:offset + limit]
            logger.info(f"path={path!r}, total_lines={len(lines)}, offset={offset}")
            return _FileReadResult(
                status="ok", path=path, content="\n".join(chunk),
                total_lines=len(lines),
            ).model_dump_json()
        except Exception as exc:
            logger.warning(f"文件读取失败: {type(exc).__name__}")
            return _FileReadResult(status="error", error=type(exc).__name__).model_dump_json()


class FileWriteTool(BaseTool):
    """工作区文本文件写入工具（由工厂函数创建，路径限制在工作区内）。"""

    name: str = "write_file"
    description: str = (
        "将文本内容写入工作区内的文件，路径相对工作区根目录；"
        "自动创建父目录，覆盖同名文件。\n"
        "何时调用：需要生成、保存或更新文本文件内容时；"
        "写入前先用 read_file 确认目标文件现状，避免误覆盖。"
        "仅支持 UTF-8 文本，路径无法越出工作区根目录。"
    )
    args_schema: type[BaseModel] = _FileWriteInput

    # 工作区根目录（Path 或 None，任意类型避免 pydantic schema 生成）
    workspace_root: Any = None

    def _run(self, *args, **kwargs) -> str:
        raise NotImplementedError("write_file 仅支持异步调用")

    async def _arun(self, path: str, content: str) -> str:
        try:
            resolved = _resolve_path(self.workspace_root, path)
            if resolved is None:
                return _FileWriteResult(
                    status="error", error="路径越出工作区根目录").model_dump_json()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            logger.info(f"path={path!r}, chars={len(content)}")
            return _FileWriteResult(
                status="ok", path=path, chars_written=len(content)).model_dump_json()
        except Exception as exc:
            logger.warning(f"文件写入失败: {type(exc).__name__}")
            return _FileWriteResult(status="error", error=type(exc).__name__).model_dump_json()


def create_file_tools(workspace_root=None) -> list[BaseTool]:
    """工厂函数：创建工作区文件工具集（glob 检索、文件读取、文件写入）。"""
    return [
        FileSearchTool(workspace_root=workspace_root),
        FileReadTool(workspace_root=workspace_root),
        FileWriteTool(workspace_root=workspace_root),
    ]
