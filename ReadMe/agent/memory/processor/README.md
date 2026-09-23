# memory.processor（快照计算）

## 职责与入口

- 所属类别：后台计算模块（不是图节点）；在固定快照上生成记忆变更计划，不写数据库、不持有写事务。
- 源码：[agent/memory/processor.py](../../../../agent/memory/processor.py)
- 调用方：`MemoryWorker._process`（[worker/README.md](../worker/README.md)）在角色会话锁内调用 `process_memory_snapshot(payload, store)`。
- 输出：`MemoryPlan`（增删改操作 + 待裁剪消息 ID），由 Worker 在短事务中提交。

## 调用链总览

| 步骤 | 函数 | 关系 |
| --- | --- | --- |
| R1 | `process_memory_snapshot(payload, store)` | Worker 直接调用 |
| R1.1 | `_parse_memory_queries(content)` | 解析检索语句 JSON |
| R1.2 | `_retrieve_memories(store, queries, final_limit)` | 混合检索并去重 |
| R1.3 | `memory_store.prepare_memory(...)` | 分块与向量编码（[../store/README.md](../store/README.md)） |
| R1.4 | `history_removals(messages, trim_index)` | 生成裁剪 ID（[../../utils/memory/README.md](../../utils/memory/README.md)） |
| R1.5 | `messages_from_dict` / `format_history` / `prepare_world_state` | 输入还原与格式化 |

## 运行链

### R1. `process_memory_snapshot`

- 定位与签名：`async def process_memory_snapshot(payload: dict, memory_store) -> MemoryPlan`，[agent/memory/processor.py:163](../../../../agent/memory/processor.py#L163)。
- 调用方与条件：`MemoryWorker._process`；只在任务通过权限核对后执行。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `payload["version"]` | `int` | 快照 | 必须为 `1` | 协议版本校验 |
| `payload["messages"]` | `list[dict]` | 快照 | 非空 | 完整消息历史（含旧消息背景） |
| `payload["new_message_start"]` | `int` | 快照 | `0 <= start < len(messages)` | 新增范围起点 |
| `payload["memory_storage_enabled"]` | `bool` | 快照 | 缺省 `True`；为假抛错 | 权限复核 |
| `payload["memory_retrieval_enabled"]` | `bool` | 快照 | 缺省 `True` | 决定是否检索旧记忆与是否允许改删 |
| `payload["world_state"]` | `dict` | 快照 | 可空 | 提示词时间/天气 |
| `memory_store` | `AsyncPostgresCharacterMemoryStore` | Worker 创建 | 必填 | 检索与预编码 |

隐式输入：`get_node_model("memory_query")`、`get_node_model("memory_summary")`、`get_prompt("memory_query")`、`get_prompt("event_summary")`、本地 Embedding 模型。

功能与内部调用：

1. 校验协议版本与存储开关；`messages_from_dict(payload["messages"])` 还原消息，校验新增范围。
2. `prepare_world_state(payload.get("world_state"))` 得到状态文本（R1.5）。
3. **检索**（`retrieval=True` 时）：取新增范围前 6 条作为查询上下文（`messages[max(0, start-6):]`），调用 `memory_query` 模型生成检索语句；`_parse_memory_queries`（R1.1）解析后 `_retrieve_memories`（R1.2）得到去重后的相关旧记忆；`final_limit=12`。
4. 组装 `<memories>` 文本：每行 `{id},{importance}: {memory}`；无检索时为空。
5. 组装 `scope` 系统消息：说明新增范围起点、旧消息只作背景、允许补充/纠正、`clean_history` 按完整历史编号并保留至少 6 条；关闭检索时追加“只提取新增记忆，不推断旧记忆内容，不更新或删除旧记录”。
6. **工具集**：完整检索时暴露 `_MEMORY_TOOLS`（insert/update/delete/clean_history）；关闭检索时只暴露 `insert_memory` 与 `clean_history`。
7. 调用 `memory_summary` 模型（绑定工具）生成操作；消息顺序为：event_summary 提示词、scope、世界状态、记忆列表、带工具标记的完整历史（`include_tools=True`）。
8. `invalid_tool_calls` 非空 → 抛 `ValueError`。
9. **校验全部工具调用**（先校验后计算，避免参数错误时做无用的切块/编码）：
   - `clean_history.index` 必须为正整数，记录 `trim_index`；
   - 其他调用名必须是 insert/update/delete；
   - update/delete：检索必须开启、`memory_id` 必须为本次检索得到的 ID、同一计划不能重复修改同一条；
   - insert/update 文本非空；`importance` 若提供必须是 0~100 的整数（insert 默认 0，update 默认 `None`）。
10. `plan = MemoryPlan(remove_ids=[item.id for item in history_removals(messages, trim_index)])`（R1.4）。
11. 对每个写操作调用 `memory_store.prepare_memory(text, importance)`（R1.3，delete 除外）生成 `MemoryOperation`；返回计划。

| 输出 | 类型 | 产生条件 | 含义 | 消费方 |
| --- | --- | --- | --- | --- |
| `operations` | `list[MemoryOperation]` | 工具调用合法 | 待提交的增删改操作及预编码 | `jobs.finish` → `store.apply_operations` |
| `remove_ids` | `list[str]` | `clean_history` 被调用 | 待裁剪消息 ID（已按工具组安全切分） | 结果行、图 `apply_results` |

副作用：多次模型请求、Embedding 编码；不写数据库。

异常与边界：任何校验失败抛 `ValueError`，由 Worker 计入失败并按退避重试；重试会重新计算，不产生部分写入（写事务在 Worker 侧）。

### R1.1. `_parse_memory_queries`

- 定位与签名：`_parse_memory_queries(content: str) -> list[dict]`，同步私有函数，[agent/memory/processor.py:19](../../../../agent/memory/processor.py#L19)。
- 行为：`strip_code_fence` 后 `json.loads`；只接受数组；元素为字符串时视为 `{"query": str}`；过滤空 query；输出 `{"query", "date_from", "date_to"}`；解析失败记录 warning 并返回 `[]`（此时不检索，仍继续总结）。

### R1.2. `_retrieve_memories`

- 定位与签名：`async def _retrieve_memories(memory_store, queries, final_limit) -> list`，[agent/memory/processor.py:51](../../../../agent/memory/processor.py#L51)。
- 行为：对每条查询用 `get_qwen_embedding_model().encode(query, prompt_name="query")` 编码，调用 `search_hybrid(embedding, query_text=query, keyword_text=query, date_from, date_to, final_limit)`；按父记忆 ID 去重合并，保持首次出现顺序。
- 空查询返回 `[]`。

### R1.3. 预编码

- 每个写操作调用 `memory_store.prepare_memory(text, importance)`：LLM 语义分块（失败回退正则）、自动提取 keywords/event_date（未显式提供时）、批量 Embedding；切块为空抛 `ValueError`。细节见 [../store/README.md](../store/README.md)。

### R1.4. 裁剪

- `history_removals(messages, trim_index)` 沿用旧裁剪规则：保留完整工具调用组，并至少保留 6 条消息；返回 `RemoveMessage` 列表，计划中只保存 ID。

### R1.5. 输入还原与格式化

- `messages_from_dict` 把快照中的序列化消息还原为 LangChain 消息对象；`format_history(messages, include_tools=True)` 输出带 `index:` 的历史文本；`prepare_world_state` 只取快照中保存的天气（时间按当前系统时间重新生成）。

## 工具契约（`_MEMORY_TOOLS`）

| 工具名 | 参数 | 校验 |
| --- | --- | --- |
| `insert_memory` | `memory: str`、`importance: int` | 文本非空；importance 0~100 |
| `update_memory` | `memory_id: int`、`new_memory: str`、`importance?: int` | ID 必须来自本次检索；文本非空；importance 0~100 |
| `delete_memory` | `memory_id: int` | ID 必须来自本次检索 |
| `clean_history` | `index: int` | 正整数；实际裁剪由 `history_removals` 保证工具组完整与最少 6 条 |

## 分支与异常链

- **关闭检索**：不调用 `memory_query` 模型、不访问旧记忆；工具集仅 insert/clean；提示词声明禁止改删旧记录。
- **检索语句解析失败**：`queries=[]`，跳过检索但仍执行总结（新增记忆照常）。
- **无 `clean_history`**：`trim_index=None`，`remove_ids=[]`。
- **校验失败**：抛错 → Worker 失败退避；不会产生部分数据库写入。
- **协议版本不符/存储关闭**：立即抛错，任务进入失败计数。

## 输入输出示例

适用 R1（新增一条记忆、裁剪旧历史）：

```text
输入 payload：new_message_start=8，memory_retrieval_enabled=True，
             messages=[... 10 条 ...]，world_state={"weather":"晴"}
模型工具调用：[insert_memory(memory="2026年9月20日 ...", importance=60),
              clean_history(index=4)]
输出 MemoryPlan：operations=[MemoryOperation("insert_memory", None, PreparedMemory(...))],
                remove_ids=["msg_1","msg_2","msg_3","msg_4"]
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 存储：[../store/README.md](../store/README.md) · 队列：[../jobs/README.md](../jobs/README.md)
- 协议：[../../classes/memory_job/README.md](../../classes/memory_job/README.md) · 提示词：[../../prompts/tools/event_summary/README.md](../../prompts/tools/event_summary/README.md)、[../../prompts/tools/memory_query/README.md](../../prompts/tools/memory_query/README.md)
- 依据：`agent/memory/processor.py`；`tests/test_memory_service.py`、`tests/classes/memory_jobs.py` 覆盖计划生成与校验；本次未执行测试。
