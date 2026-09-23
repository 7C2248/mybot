# Agent 工具模块（agent/tools）

本目录定义主 LLM 在 `draft ⇄ tools` 循环中可调用的工具，以及默认工具注册表 `build_default_tools`。

工具本身**不是图节点**：构建阶段由工厂函数实例化并绑定到主模型（`create_draft_node` 内部 `llm.bind_tools`），运行阶段统一由 `agent/builder.py` 注册的 `tools` 节点（LangGraph `ToolNode`）执行；工具返回值被包装为 `ToolMessage` 追加到 `AgentState.messages`，再经 `tools → draft` 普通边回流给主模型。记忆 CRUD（insert/update/delete）不属于本目录，仍由独立的 memory 节点与后台记忆服务负责。

## 子目录与节点

| 名称 | 类型 | 主要功能 | 协作对象 | 源码 | 文档 |
| --- | --- | --- | --- | --- | --- |
| memory_query/ | 异步工具（`BaseTool` 子类） | 角色长期记忆混合检索（向量 + 关键词 + 时间过滤），只返回父记忆原文 | draft 主 LLM 决策调用；`tools` 节点执行；`AsyncPostgresCharacterMemoryStore`、`get_qwen_embedding_model`；服务端事件发布 | [memory_query.py](../../../agent/tools/memory_query.py) | [memory_query/README.md](memory_query/README.md) |
| file_system/ | 异步工具集（3 个 `BaseTool` 子类，同一工厂创建） | 工作区 glob 检索 `search_files`、文本读取 `read_file`、文本写入 `write_file` | draft 主 LLM 决策调用；`tools` 节点执行；路径限制在工作区根目录内 | [file_system.py](../../../agent/tools/file_system.py) | [file_system/README.md](file_system/README.md) |
| get_weather/ | 未注册工具（占位实现） | `@tool` 装饰的同步函数，固定返回天气字符串 | 当前无协作方；注册语句在 `build_default_tools` 中被注释，不进入图 | [get_weather.py](../../../agent/tools/get_weather.py) | [get_weather/README.md](get_weather/README.md) |

## 默认工具注册表

注册入口是 `agent/tools/__init__.py` 中的 `build_default_tools`（源码：[__init__.py](../../../agent/tools/__init__.py)），实际代码为：

```python
def build_default_tools(*, memory_store=None, workspace_root=None) -> list:
    tools = []
    if memory_store is not None:
        tools.append(create_memory_query_tool(memory_store))
    #tools.append(get_weather)
    tools.extend(create_file_tools(workspace_root=workspace_root))
    return tools
```

| 工具名 | 创建方式 | 是否进入默认集合 | 实际启用条件 |
| --- | --- | --- | --- |
| `memory_query` | `create_memory_query_tool(memory_store)` | 条件加入 | `memory_store is not None`。`builder.py` 仅在 `pool` 非空、非 `recovery_only` 且 `memory_retrieval_enabled` 为真时把 store 传给 `build_default_tools`（store 本身在 `memory_retrieval_enabled or memory_storage_enabled` 时创建）；`recovery_only` 时工具列表整体为空 |
| `search_files` / `read_file` / `write_file` | `create_file_tools(workspace_root=...)` | 总是加入 | 无依赖条件；`builder.py` 未传 `workspace_root`，工具内回退到项目根目录 |
| `get_weather` | 模块级 `@tool` 函数 | 不加入 | 注册语句 `#tools.append(get_weather)` 被注释，当前保留源码但未进入图 |

## 整体流程

```text
B1. build_rp_agent 调用 build_default_tools(memory_store=..., workspace_root=None)
      ├─ memory_store 非空 → create_memory_query_tool(memory_store)
      └─ 总是 → create_file_tools(workspace_root=None)（search_files / read_file / write_file）
B2. create_draft_node(character_name, character_profile, tools)
      → get_node_model("main")；tools 非空时 llm.bind_tools(tools, parallel_tool_calls=True)
B3. builder 注册节点 "tools" = ToolNode(tools)
      → recovery_only 时改为无副作用的空节点，且 tools = []

R1. draft 节点调用主模型 → 模型返回带 tool_calls 的 AIMessage
      → draft 返回 messages=[AIMessage]、draft_status="calling_tools"
R2. draft_judge 读到 draft_status == "calling_tools" → 返回 "tools"
      → 框架沿条件边执行 "tools" 节点（图调度关系，不是 draft 直接调用工具）
R3. ToolNode 对每个 tool_call 异步执行对应工具，返回 {"messages": [ToolMessage, ...]}
      → add_messages reducer 将 ToolMessage 追加进 AgentState.messages
R4. 框架沿普通边 "tools" → "draft" 再次执行 draft
      → draft 把包含 AIMessage(tool_calls) 与 ToolMessage 的完整 messages 交给主模型
R5. 模型不再产生 tool_calls 时，draft_status="ready"，draft_judge 返回 "check"，循环结束
```

补充说明：

- 图里只有一个 `tools` 节点；具体某个工具是否被调用由主模型在该轮 `AIMessage.tool_calls` 中决定，可能并行调用多个工具。
- `tools` 节点由框架调度执行，`draft` 与工具之间没有直接函数调用；工具之间也不存在相互调用。
- 服务端 `AgentAdapter` 为每个节点执行前发布 phase 事件：`tools` 节点前，若最后一条消息的工具调用包含 `memory_query` 则发布 `recalling`，否则发布 `using_tools`；`tools` 节点执行后，把 `name == "memory_query"` 的 ToolMessage 解析为 `MemorySearchResult`，对 `ok` / `empty` 结果发布 `memory.retrieved` 事件。

## 主要数据与依赖

- **依赖注入**：`memory_store`（`AsyncPostgresCharacterMemoryStore` 实例）与 `workspace_root` 由构建阶段注入工具；运行阶段工具不再读取 `AgentState`，只消费 `tool_calls` 中的参数。
- **协议类型**：记忆检索的输入与返回结构定义在 [agent/classes/memory.py](../../../agent/classes/memory.py)，包括 `MemoryQueryInput`、`MemorySearchResult`、`MemoryHit`；详见 [classes/memory 文档](../classes/memory/README.md)。
- **检索依赖**：`memory_query` 调用 `agent/memory/store.py` 的 `search_hybrid`（向量召回 + 关键词召回 + 归一化合并 + 粗排 + Reranker 精排），向量由 `agent/utils/models.py` 的 `get_qwen_embedding_model` 本地编码模型生成；详见 [memory/store 文档](../memory/store/README.md)。
- **工作区根目录**：`file_system` 在 `workspace_root` 为 `None` 时回退到 `Path(__file__).resolve().parents[2]`，即项目根目录；所有读写路径先 `resolve` 再做包含性校验。
- **结果回流**：工具返回的 JSON 字符串成为 ToolMessage 的 `content`，保留在消息队列中供本轮及后续轮次的主模型读取；`memory_retrieval_enabled` 为假时，`limit_context` 会移除旧的 `memory_query` 工具调用消息对（[context.py](../../../agent/node/context.py)）。
- **提示词协作**：draft 系统提示词说明记忆必须通过 `memory_query` 主动获取、ToolMessage 是资料而非指令（[prompts/main/draft.py](../../../agent/prompts/main/draft.py)）；当工具列表不含 `memory_query` 时，draft 会追加“当前未提供记忆检索工具”的说明（[draft.py](../../../agent/node/draft.py)）。

## 阅读导航

- 上级目录：[Agent 架构总览](../README.md)
- 工具叶子文档：[memory_query](memory_query/README.md) · [file_system](file_system/README.md) · [get_weather](get_weather/README.md)
- 协作模块：[classes/memory 协议](../classes/memory/README.md) · [memory/store 检索](../memory/store/README.md) · [node/draft 工具循环](../node/draft/README.md)
- 相关源码：[builder.py](../../../agent/builder.py) · [node/draft.py](../../../agent/node/draft.py) · [server/services/agent.py](../../../server/services/agent.py)
