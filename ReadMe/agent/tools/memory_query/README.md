# memory_query 工具

## 职责与入口

- 所属类别：`agent/tools` 目录下的异步检索工具，`langchain_core.tools.BaseTool` 的子类；**不是图节点**。
- 源码文件：[agent/tools/memory_query.py](../../../../agent/tools/memory_query.py)；工具名（模型看到的函数名）为 `memory_query`。
- 职责：让主 LLM 在 `draft ⇄ tools` 循环中自主查询角色长期记忆库，执行一次“日志体关键词 + 时间过滤”的混合检索，返回父记忆原文；结果以 ToolMessage 形式进入对话上下文，不注入 system prompt。
- 工厂函数：`create_memory_query_tool(memory_store)`，返回 `CharacterMemoryQueryTool` 实例。
- 调用方式：由 `agent/builder.py` 注册的 `tools` 节点（LangGraph `ToolNode`）在收到主模型的 `memory_query` 工具调用后异步执行 `_arun`。
- 上游：draft 节点中的主模型；下游：ToolMessage 经 `tools → draft` 边回流给主模型，服务端另发布阶段与检索事件。

## 调用链总览

```text
构建阶段
B1. create_memory_query_tool(memory_store)
      → CharacterMemoryQueryTool(memory_store=memory_store)
B2. build_default_tools(memory_store=...)：memory_store 非空时把工具加入默认列表
B3. create_draft_node：llm.bind_tools(tools, parallel_tool_calls=True)
      同时 builder 注册节点 "tools" = ToolNode(tools)

运行阶段
R1. draft 调用主模型 → AIMessage(tool_calls=[{name:"memory_query", args:{...}}])
      → draft 返回 draft_status="calling_tools"
R2. draft_judge 返回 "tools" → 框架调度 "tools" 节点（非直接调用）
R3. ToolNode 校验参数并异步调用 CharacterMemoryQueryTool._arun(...)
      R3.1 store 可用性检查 → R3.2 MemoryQueryInput 二次校验 → R3.3 生成查询向量
      → R3.4 search_hybrid 混合检索 → R3.5 组装 MemoryHit 去重 → R3.6 序列化返回
R4. 返回 JSON 字符串 → ToolMessage(content=..., name="memory_query", tool_call_id=...)
      → add_messages 追加到 AgentState.messages
R5. 框架沿 "tools" → "draft" 边再次执行 draft，主模型读到 ToolMessage
      不再发起工具调用时 draft_status="ready"，draft_judge 返回 "check"
```

## 构建链

### B1. create_memory_query_tool

- 定位与签名：`create_memory_query_tool(memory_store) -> CharacterMemoryQueryTool`，同步函数，源码 [memory_query.py](../../../../agent/tools/memory_query.py)。
- 调用方与条件：`agent/tools/__init__.py` 的 `build_default_tools` 在 `memory_store is not None` 时调用；`builder.py` 只在 `pool` 非空、非 `recovery_only` 且 `memory_retrieval_enabled` 为真时把 store 传入 `build_default_tools`（store 本身在 `memory_retrieval_enabled or memory_storage_enabled` 时创建）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `memory_store` | `AsyncPostgresCharacterMemoryStore` 实例（源码未标注类型，声明为 `Any`） | 必填（位置参数） | `build_rp_agent` 中经 `AsyncPostgresCharacterMemoryStore.create(pool, character_name)` 创建；绑定到该角色专属记忆表 |

功能与内部调用：工厂体只有一行 `return CharacterMemoryQueryTool(memory_store=memory_store)`，不做参数校验，也不访问数据库。

输出：一个 `CharacterMemoryQueryTool` 实例，其固定属性为：

| 属性 | 值 | 说明 |
| --- | --- | --- |
| `name` | `"memory_query"` | 主模型发起工具调用时使用的函数名，也是服务端识别事件的消息名 |
| `description` | 长文本（何时调用、query 构造、日期区间规则、ok/empty/error 语义） | 作为工具说明提供给模型；不在此复述全文 |
| `args_schema` | `MemoryQueryInput` | 见 [classes/memory 协议](../../classes/memory/README.md) |
| `memory_store` | 工厂传入的实例 | 运行时检索用的唯一外部依赖 |

副作用、异常与去向：无副作用、无显式异常；`memory_store=None` 时仍可构造，但运行时进入 R3 的错误分支。实例进入工具列表后由 `create_draft_node` 绑定到主模型，并由 `ToolNode` 持有同名工具用于执行。

### B2. 注册与绑定

- `build_default_tools(memory_store=...)`：`memory_store` 非空才追加 `memory_query`；因此 `memory_retrieval_enabled=False`、`pool=None` 或 `recovery_only=True` 时，工具列表不含本工具（`recovery_only` 时列表整体为空）。详见父文档 [tools/README.md](../README.md)。
- `create_draft_node`（[draft.py](../../../../agent/node/draft.py)）：`tools` 非空时执行 `llm.bind_tools(tools, parallel_tool_calls=True)`；若绑定列表中不含 `memory_query`，draft 会在系统提示词后追加“当前未提供记忆检索工具；使用已有消息中的记忆，缺少依据时保留未知，不假称已经检索。”。
- `builder.py` 注册节点 `"tools"`：`ToolNode(tools)`（[builder.py](../../../../agent/builder.py)）；`recovery_only=True` 时该节点被替换为返回 `{}` 的空节点。

## 运行链

### R1. 主模型发起工具调用（draft 内）

- 调用方：draft 节点 `create_draft_node.<locals>.node`，本轮主模型 `llm.ainvoke([SystemMessage] + messages)` 返回的 `AIMessage` 携带 `tool_calls`。
- 触发条件：模型决定调用 `memory_query`。draft 会校验每个调用的 `id` 存在且唯一、`name` 在当前绑定工具集合内；否则抛出 `ValueError` 并进入 draft 自身的重试循环（最多 4 次尝试、3 次重试，重试间隔 1 秒），耗尽后 `draft_status="failed"` 去 `reply_failed`。
- 输出：draft 返回 `messages=[AIMessage]`、`draft_status="calling_tools"`；随后由 `draft_judge` 路由，draft 不直接调用工具。draft 内部细节见 [node/draft 文档](../../node/draft/README.md)。

### R2. draft_judge 路由到 tools 节点

- 定位与签名：`draft_judge(state) -> str`，同步函数，源码 [draft.py](../../../../agent/node/draft.py)。
- 调用方与条件：框架在 `draft` 节点执行后按条件边调用。
- 功能：读取 `state["draft_status"]`；`"calling_tools"` 返回 `"tools"`，`"ready"` 返回 `"check"`，其余返回 `"reply_failed"`。
- 输出与去向：`builder.py` 中 `add_conditional_edges("draft", draft_judge, {"tools": "tools", "check": "check", "reply_failed": "reply_failed"})`；返回 `"tools"` 时框架调度 `tools` 节点执行本工具。

### R3. ToolNode 执行 CharacterMemoryQueryTool

- 定位与签名：`CharacterMemoryQueryTool._arun(self, query: str, date_from: Optional[str] = None, date_to: Optional[str] = None, limit: int = 12) -> str`，异步方法，源码 [memory_query.py](../../../../agent/tools/memory_query.py)。
- 调用方与条件：`tools` 节点 `ToolNode(tools)` 对最后一个 AIMessage 中的每个 tool_call 调用 `tool.ainvoke(call_args)`，由 `BaseTool.arun` 在参数校验后调用 `_arun`。
- 参数先经 `args_schema=MemoryQueryInput` 校验：`BaseTool` 会把校验后的字段值作为关键字参数传入，因此 `date_from` / `date_to` 运行时实际可能是 `datetime.date` 对象（注解写的是 `Optional[str]`），`_arun` 内部会再次校验并统一转换。参数不合法时抛出的 pydantic `ValidationError` 不会进入 `_arun`，而由 ToolNode 转成 `status="error"` 的 ToolMessage（见“分支与异常链”）。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `query` | `str` | 模型生成的 tool_call args | 必填；去空白后非空；建议不超过 30 字符的日志体关键词序列 | 同时作为向量编码文本与关键词检索文本 |
| `date_from` | `datetime.date` 或 `str` | 同上 | 可省略；与 `date_to` 必须成对提供 | 事件起始日期过滤（含当天），序列化回 `yyyy-mm-dd` 字符串 |
| `date_to` | `datetime.date` 或 `str` | 同上 | 可省略；不得早于 `date_from` | 事件结束日期过滤（含当天） |
| `limit` | `int` | 同上 | 默认 12（`_DEFAULT_FINAL_LIMIT`）；严格整数，范围 1–20 | 传给 `search_hybrid` 的 `final_limit`，即最终返回的记忆条数上限 |

隐式输入：

- 闭包/实例依赖：`self.memory_store`，构建阶段注入的角色记忆存储实例；`None` 时触发错误分支。
- 全局依赖：`get_qwen_embedding_model()`（[agent/utils/models.py](../../../../agent/utils/models.py)），`lru_cache` 缓存的本地 SentenceTransformer 编码模型，首次调用时加载权重，路径缺失会抛 `FileNotFoundError`。
- 日志器：`utils.daily_logger.get_logger("memory.query")`。
- 模块常量：`_DEFAULT_FINAL_LIMIT = 12`。

功能与内部调用（按执行顺序）：

1. **store 可用性检查**：`self.memory_store is None` 时 `raise RuntimeError("memory store unavailable")`，被外层 `except Exception` 捕获。
2. **参数二次校验与序列化**：`MemoryQueryInput(query=..., date_from=..., date_to=..., limit=...).model_dump(mode="json")`。该校验会去空白并拒绝空 query、拒绝日期单边提供或倒置、拒绝越界 limit；`mode="json"` 把 `date` 转为 `yyyy-mm-dd` 字符串，便于后续 SQL 参数与日志使用。
3. **生成查询向量**：`encoder = get_qwen_embedding_model()`；`embedding = encoder.encode(params["query"], prompt_name="query")`，得到 `list[float]`。
4. **混合检索**：`await self.memory_store.search_hybrid(embedding, query_text=params["query"], keyword_text=params["query"], date_from=params["date_from"], date_to=params["date_to"], final_limit=params["limit"])`。这里查询向量与关键词文本使用同一 `query`；`search_hybrid` 的完整契约（向量召回 → 关键词召回 → 归一化合并 → 粗排 → Reranker 精排，时间过滤对 `event_date` 严格匹配，返回按精排顺序的父记忆行列表）见 [memory/store 文档](../../memory/store/README.md)。返回空列表表示无命中。
5. **组装命中并去重**：遍历 `rows`，用 `MemoryHit(memory_id=row["id"], text=row["memory"], event_date=str(row["event_date"]) if row.get("event_date") else None, update_time=str(row["update_time"]) if row.get("update_time") else None)` 构造命中，并以 `hits[hit.memory_id] = hit` 按父记忆 ID 去重（重复 ID 后写覆盖前写，只保留一条）。工具只取父记忆 `id`、`memory`、`event_date`、`update_time` 四个字段，不返回子表 chunk 或分数。
6. **记录日志**：`logger.info` 输出 query、日期区间与父记忆命中数。
7. **序列化返回**：`MemorySearchResult(status="ok" if hits else "empty", hits=list(hits.values())).model_dump_json()`。`ok` 必须至少一条命中；无命中时 `status="empty"` 且 `hits=[]`。
8. **异常兜底**：以上任意步骤抛出的异常都会被 `except Exception as exc` 捕获，记录 `logger.warning(f"记忆检索失败: {type(exc).__name__}")`，返回 `MemorySearchResult(status="error", hits=[], error=type(exc).__name__).model_dump_json()`；只暴露异常类名，不包含消息或堆栈。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| 返回字符串（JSON） | `str` | 总是返回，不向框架抛异常 | 序列化的 `MemorySearchResult`，含 `status` / `hits` / `error` | `ToolNode` 包装为 ToolMessage 的 `content` |
| `status` | `"ok"` / `"empty"` / `"error"` | 有命中 / 无命中 / 任意异常 | 检索结果状态；服务端只对 `ok`、`empty` 发布 `memory.retrieved` 事件 | 主模型（读取 ToolMessage）；`server/services/agent.py` |
| `hits` | `list[MemoryHit]` | 仅 `ok` 时非空 | 每条含 `memory_id`（父记忆主键）、`text`（父记忆原文）、`event_date`、`update_time`（可为 `null`） | 主模型；服务端事件 |
| `error` | `str \| None` | 仅 `error` 时为异常类名 | 失败类型标识，便于模型判断“未能确认”而非“不存在” | 主模型 |

副作用：首次调用触发本地 Embedding 模型加载（进程内 `lru_cache` 缓存，后续调用复用）；对记忆库执行只读查询；写入一条 info/warning 日志。不修改 `AgentState`，不写数据库。

异常与边界：工具内部不抛出异常（全部转成 `status="error"`），也不重试、不设超时；日期与 limit 的非法值在进入 `_arun` 前由 schema 拒绝；`memory_store=None`、模型文件缺失、数据库/检索异常都归入 `error`。检索为空是正常结果（`empty`），不代表事件从未发生。

后续去向：返回值由 `ToolNode` 包装为 `ToolMessage(content=<JSON>, name="memory_query", tool_call_id=<对应调用 id>)`，通过 `add_messages` reducer 追加到 `AgentState.messages`；框架沿 `tools → draft` 边再次执行 draft，主模型在下一轮上下文里读取该 ToolMessage。

### R4. 结果回流与再次生成

- `ToolNode` 输出 `{"messages": [ToolMessage, ...]}`；一次 AIMessage 中的多个并行工具调用会生成多条 ToolMessage。
- draft 再次执行时，`messages` 已包含原始 AIMessage(tool_calls) 与 ToolMessage；`node` 将它们原样交给主模型（[draft.py](../../../../agent/node/draft.py)），因此本轮与后续轮次都能读取检索原文。
- 服务端在流式更新中处理 `tools` 节点输出（[server/services/agent.py](../../../../server/services/agent.py)）：
  - 执行前发布 phase：最后一条消息的工具调用含 `memory_query` → `"recalling"`，否则 → `"using_tools"`；
  - 执行后把 `name == "memory_query"` 的 ToolMessage 用 `MemorySearchResult.model_validate_json` 解析，`status` 为 `ok` / `empty` 时发布 `"memory.retrieved"`，事件字段为 `id`（字符串化的 memory_id）、`memory`、`event_date`、`update_time`；解析失败或 `status="error"` 时不发布。
- 当 `memory_retrieval_enabled=False` 时，`limit_context` 会删除历史中由 `memory_query` 产生的 AIMessage 与 ToolMessage（[context.py](../../../../agent/node/context.py)）。

### R5. draft_judge 再次路由

- 主模型不再返回 `tool_calls` 时，draft 返回 `draft_status="ready"`，`draft_judge` 返回 `"check"`，框架进入检查节点；若模型连续多轮调用工具，则重复 R1–R4，直到模型给出正文或重试耗尽。

## 分支与异常链

| 条件 | 处理函数与行为 | 输出 | 去向 |
| --- | --- | --- | --- |
| 工具参数不满足 `MemoryQueryInput`（空 query、日期单边/倒置、limit 越界等） | `BaseTool` 校验阶段抛 `ValidationError`，不进入 `_arun`；ToolNode 默认错误处理把它转为错误 ToolMessage | `ToolMessage(status="error", name="memory_query")`，content 为校验错误说明 | 追加到 messages，draft 再次生成时可换参数重查 |
| `memory_store is None` | `_arun` 抛 `RuntimeError("memory store unavailable")` 并被自身捕获 | `{"status":"error","hits":[],"error":"RuntimeError"}` | 同上；正常构建路径不会出现该分支 |
| Embedding 模型缺失或编码失败 | `_arun` 捕获异常 | `error` 为 `FileNotFoundError` 等异常类名 | 同上 |
| `search_hybrid` 抛异常（数据库连接、SQL、Reranker 内部等） | `_arun` 捕获异常；`search_hybrid` 内部精排失败时自身会回退粗排结果，不抛到工具层（见 [store 文档](../../memory/store/README.md)） | `error` 为异常类名；精排回退时仍为 `ok` | 同上 |
| 检索无命中 | 正常返回 | `{"status":"empty","hits":[],"error":null}` | 模型可更换关键词/日期重查，或按“未能确认”处理 |
| 检索依赖被禁用（`memory_retrieval_enabled=False`） | 工具不加入列表，模型无法发起调用；draft 系统提示词注明未提供记忆检索工具 | 无工具消息 | 模型使用已有上下文；历史工具消息对由 `limit_context` 清理 |
| `recovery_only=True`（检查点修复） | `tools=[]`，`tools` 节点为空节点 | 无工具执行 | 不产生新的工具调用 |
| 工具调用 ID 缺失/重复或工具名不在绑定列表 | draft 在发起阶段就抛 `ValueError`，进入 draft 重试循环（最多 4 次尝试、3 次重试）；耗尽后 `draft_status="failed"` | `reply_failed` 节点清理失败轮消息 | 图结束，等待用户重试 |

## 输入输出示例

示例 1（步骤 R3，正常命中，参考 `tests/test_reply_memory.py::test_query_result_contains_parent_records_and_explicit_errors`）：

输入（tool_call args）：

```json
{"query": "公园 约定", "date_from": "2026-09-01", "date_to": "2026-09-30", "limit": 12}
```

`search_hybrid` 返回两行相同父记忆（示例数据）：

```json
[{"id": 7, "memory": "周六去公园。", "event_date": "2026-09-11"},
 {"id": 7, "memory": "周六去公园。", "event_date": "2026-09-11"}]
```

工具返回（ToolMessage content）：

```json
{"status":"ok","hits":[{"memory_id":7,"text":"周六去公园。","event_date":"2026-09-11","update_time":null}],"error":null}
```

示例 2（步骤 R3，无命中）：`search_hybrid` 返回 `[]` →

```json
{"status":"empty","hits":[],"error":null}
```

示例 3（步骤 R3，store 不可用）：`create_memory_query_tool(None).ainvoke({"query": "约定"})` →

```json
{"status":"error","hits":[],"error":"RuntimeError"}
```

## 关联文档与验证依据

- 上级目录：[tools/README.md](../README.md)
- 同级工具：[file_system](../file_system/README.md) · [get_weather](../get_weather/README.md)
- 共享协议：[classes/memory（MemoryQueryInput / MemorySearchResult / MemoryHit）](../../classes/memory/README.md)
- 检索实现：[memory/store（search_hybrid）](../../memory/store/README.md)
- 图内循环：[node/draft（bind_tools、draft_judge、工具结果回流）](../../node/draft/README.md)
- 源码：[memory_query.py](../../../../agent/tools/memory_query.py) · [tools/__init__.py](../../../../agent/tools/__init__.py) · [builder.py](../../../../agent/builder.py) · [draft.py](../../../../agent/node/draft.py) · [server/services/agent.py](../../../../server/services/agent.py)
- 已有测试覆盖（静态核对，未在本次文档编写中重新运行）：`tests/test_reply_memory.py`（结果去重与字段、显式错误、无 store 不绑定）、`tests/test_conversation_policy.py`（检索开关与上下文清理）、`tests/test_module_layout.py`（工具名与实例类型）、`tests/test_rp_pipeline.py`（工具循环）。
