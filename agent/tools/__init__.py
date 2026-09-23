"""角色对话使用的工具集合与默认工具注册表。

主 LLM 绑定 build_default_tools 返回的全部工具；
记忆 CRUD（insert/update/delete）不属于本目录，仍由独立 memory 节点负责。
"""

from .file_system import create_file_tools
from .get_weather import get_weather
from .memory_query import CharacterMemoryQueryTool, create_memory_query_tool

__all__ = [
    "CharacterMemoryQueryTool",
    "create_memory_query_tool",
    "create_file_tools",
    "build_default_tools",
]


def build_default_tools(*, memory_store=None, workspace_root=None) -> list:
    """组装主 LLM 可用的全部工具：记忆检索、天气查询与工作区文件检索/读写。

    工具目录内所有工具默认全部暴露；依赖缺失时跳过对应工具
    （memory_store 为 None 时不提供 memory_query）。
    """
    tools = []
    if memory_store is not None:
        tools.append(create_memory_query_tool(memory_store))
    #tools.append(get_weather)
    tools.extend(create_file_tools(workspace_root=workspace_root))
    return tools
