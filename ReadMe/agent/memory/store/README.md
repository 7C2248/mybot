# memory.store（角色记忆存储与混合检索）

## 职责与入口

- 所属类别：存储实现（不是图节点）。
- 源码：[agent/memory/store.py](../../../../agent/memory/store.py)
- 对外类型：`AsyncPostgresCharacterMemoryStore`，由 `agent/memory/__init__.py` 导出。
- 使用方：
  - `memory_query` 工具（[../../tools/memory_query/README.md](../../tools/memory_query/README.md)）；
  - 后台 `process_memory_snapshot` 的检索与预编码（[../processor/README.md](../processor/README.md)）；
  - `MemoryWorker` 提交记忆（[../worker/README.md](../worker/README.md)）；
  - 维护脚本 `scripts/rebuild_character_memory.py`。

## 表结构

| 表 | 列 | 说明 |
| --- | --- | --- |
| `{character_name}`（父表） | `id SERIAL PK`、`memory TEXT`、`update_time TIMESTAMP`、`importance INTEGER`、`event_date DATE`、`keywords TEXT` | 每条长期记忆的原文与元数据 |
| `{character_name}_chunks`（子表） | `id SERIAL PK`、`parent_id`（外键级联删除）、`chunk_text`、`chunk_index`、`embedding vector(1024)` | 语义分块与向量，供混合检索 |

## 调用链总览

| 阶段 | 步骤 | 关系 |
| --- | --- | --- |
| 构建 | B1 `create` → `_init_table` | 确保父/子表、外键与索引存在 |
| 运行 | R1 `prepare_memory` | 分块 + Embedding（不写库） |
| 运行 | R2 `apply_operations` | 事务内执行增删改并收集审计 |
| 运行 | R3 `_commit_operation` / `insert_memory` / `update_memory` / `delete_memory` / `update_memory_top_field` | 常规写入口 |
| 运行 | R4 `search_hybrid` → `_rerank` | 混合检索与精排 |
| 运行 | R5 `write_session` / `get_memories` | 角色锁与读取 |

## 构建链

### B1. `create` / `_init_table`

- 定位与签名：`AsyncPostgresCharacterMemoryStore.create(pool, character_name)`，异步工厂，[agent/memory/store.py:72](../../../../agent/memory/store.py#L72)；`_init_table()`，[agent/memory/store.py:78](../../../../agent/memory/store.py#L78)。
- 行为：注册 pgvector 类型；`CREATE TABLE IF NOT EXISTS` 父表与子表；幂等补充外键 `{chunk_table}_parent_id_fkey`（`ON DELETE CASCADE`）；创建 `parent_id`、`chunk_index`、父表 `event_date` 索引。
- 输出：存储实例，持有 `pool`、`character_name`、`chunk_table`。异常：数据库不可用/DDL 失败向上抛出。

## 运行链

### R1. `prepare_memory`

- 定位与签名：`async def prepare_memory(self, memory: str, importance: int | None = 0, event_date: str = None, keywords: str = None) -> PreparedMemory`，[agent/memory/store.py:146](../../../../agent/memory/store.py#L146)。
- 行为：
  1. `chunk_memory(memory)`（[../../utils/chunking/README.md](../../utils/chunking/README.md)）得到 `chunks` 与自动提取的 `auto_keywords`/`auto_event_date`；LLM 不可用时回退正则切割。
  2. 在线程中调用 `get_qwen_embedding_model().encode(text)` 对每个非空块编码。
  3. 显式传入的 `event_date`/`keywords` 优先于自动提取结果。
  4. 没有任何有效块时抛 `ValueError("记忆切块为空...")`。
- 输出：`PreparedMemory(text, importance, event_date, keywords, chunks=[(text, embedding)])`；本方法**不写库、不持有写事务**，可安全在长计算阶段调用。

### R2. `apply_operations`

- 定位与签名：`async def apply_operations(self, conn, operations: list[MemoryOperation], *, strict=True, changes: list | None = None) -> list`，[agent/memory/store.py:166](../../../../agent/memory/store.py#L166)。
- 调用方：`jobs.finish`（Worker 事务）与 `_commit_operation`。
- 行为（逐操作）：
  - `delete_memory`：`DELETE ... RETURNING` 父行；`strict` 且不存在抛 `ValueError`；审计记录 `before`。
  - 其他操作缺少 `prepared` 抛 `ValueError`。
  - `insert_memory`：插入父行（`update_time=datetime.now()`、`importance or 0`）。
  - `update_memory`：先 `SELECT ... FOR UPDATE` 记录 `before`，再 `UPDATE`（`importance = COALESCE(%s, importance)`），随后删除旧子块并重新插入新块。
  - 未知操作名抛 `ValueError`。
  - 父行不存在时 `strict=False` 记录 `None` 并跳过。
  - 每个写操作把子块按 `chunk_index` 顺序插入。
- 输出：每行结果列表；`changes` 收集审计（操作名 + before/after 完整父行）。**调用方必须在事务提交后**再调用 `log_changes` 输出日志。

### R3. 常规写入口

| 方法 | 签名 | 行为 |
| --- | --- | --- |
| `_commit_operation` | `(operation)` | 借用连接 → `write_session` 角色锁 → 事务内 `apply_operations(strict=False)` → 提交后 `log_changes(source="direct")` |
| `insert_memory` | `(memory, importance=0, event_date=None, keywords=None)` | `prepare_memory` + `_commit_operation("insert_memory")` |
| `update_memory` | `(memory_id, new_memory, importance=None, event_date=None, keywords=None)` | `prepare_memory` + `_commit_operation("update_memory")` |
| `delete_memory` | `(memory_id)` | `_commit_operation("delete_memory")`；不存在时 `strict=False` 返回 `False` |
| `update_memory_top_field` | `(memory_id, update_items: dict)` | 空字典抛 `ValueError`；锁定父行后按 key 更新字段（如重编码），提交后记录审计 |

- `log_changes`（[agent/memory/store.py:228](../../../../agent/memory/store.py#L228)）：记录“记忆变更已提交”，含操作、角色、记忆 ID 与完整 `before`/`after`；不写向量块。
- `write_session`（[agent/memory/store.py:135](../../../../agent/memory/store.py#L135)）：`pg_advisory_lock(hashtextextended("memory-character:{角色}", 0))`，覆盖常规写与后台整理，避免同一角色库并发修改。

### R4. `search_hybrid`

- 定位与签名：`async def search_hybrid(self, query_embedding, query_text=None, keyword_text=None, date_from=None, date_to=None, use_reranker=True, rerank_top_k=50, half_life_seconds=..., similarity_threshold=0.55, vector_limit=30, keyword_limit=30, final_limit=15, vector_weight=0.55, keyword_weight=0.25, importance_weight=0.10, time_weight=0.10)`，[agent/memory/store.py:312](../../../../agent/memory/store.py#L312)。
- 调用方：`memory_query` 工具（`final_limit` 由工具参数决定，默认 12）、`processor._retrieve_memories`（`final_limit=12`）。

功能：

1. `keyword_text` 按 `[,，\s]+` 拆词，最多取前 5 个。
2. **向量召回**（子表）：`MAX(1 - (embedding <=> 查询向量)) >= similarity_threshold`，按 `parent_id` 聚合取最大分，`ORDER BY vector_score DESC LIMIT vector_limit`；时间过滤下推到父表（`event_date >= / <=`，NULL 日期在时间检索中排除）。
3. **关键词召回**（子表 JOIN 父表）：`chunk_text %% 关键词`（trigram）或 `keywords ILIKE %关键词%`，分数取 `GREATEST(similarity(chunk_text, kw)...)`；同样的时间过滤；`LIMIT keyword_limit`。
4. **合并归一化**：按父记忆 ID 合并两路分数；各自除以本路最高分。
5. **粗排**：`coarse_score = v_norm*vector_weight + k_norm*keyword_weight + (importance/100)*importance_weight + decay*time_weight`；`decay = 1/(1 + age/half_life)`，基准 `update_time`（时区感知时用 UTC 计算），因此重要性与时间衰减只计一次。
6. **精排**：`use_reranker` 且 `query_text` 非空且候选多于 1 条时调用 `_rerank`；否则返回粗排前 `final_limit`。

### R4.1. `_rerank`

- 定位与签名：`async def _rerank(self, rows, query_text, rerank_top_k, final_limit)`，[agent/memory/store.py:502](../../../../agent/memory/store.py#L502)。
- 行为：取粗排前 `rerank_top_k` 条构造 `Document(page_content=memory, metadata={"idx": i})`；通过 `_get_reranker(top_k=final_limit)` 获取 `CrossEncoderReranker`（[../../classes/reranker/README.md](../../classes/reranker/README.md)）并调用 `acompress_documents`；按返回顺序用 `idx` 重建结果（避免相同文本被去重误杀）；精排失败回退粗排；数量不足时按粗排补齐。
- `_get_reranker(top_k=50)`：实例级薄缓存；实际权重由 [../../utils/models/README.md](../../utils/models/README.md) 的 `get_reranker_model` 按 `top_k` 缓存。

### R5. 读取与锁

| 方法 | 签名 | 行为 |
| --- | --- | --- |
| `get_memories` | `(limit=None)` | 按 `id ASC` 返回父表记忆；`limit > 0` 时限制条数；不访问子表 |
| `write_session` | 异步上下文管理器 | 角色级 advisory lock，所有常规写入口与后台整理共用 |

| 输出 | 类型 | 产生条件 | 含义 | 消费方 |
| --- | --- | --- | --- | --- |
| `PreparedMemory` | dataclass | `prepare_memory` 成功 | 文本、重要性、日期、关键词、已编码块 | `apply_operations` |
| 父行 dict | dict | 写操作成功 | 已提交的记忆记录 | 工具/脚本/日志 |
| 检索行列表 | list[dict] | `search_hybrid` | 含 `id/memory/importance/event_date/update_time/keywords`（及分数） | `memory_query`、processor |

副作用：写库（父/子表）、advisory lock、Embedding/Reranker 模型调用。

异常与边界：`prepare_memory` 空切块抛错；`strict=True` 时删除/更新不存在的记忆抛错；检索为空返回 `[]`；Reranker 异常降级粗排。

## 分支与异常链

- **LLM 分块不可用**：`chunk_memory` 回退正则切割与正则关键词/日期提取。
- **关闭检索的会话**：后台只调用 `insert_memory`，不检索旧记忆；`memory_query` 工具不注册。
- **并发写**：角色锁串行化；提交使用短事务，计算阶段不持写事务。
- **重编码维护**：`update_memory_top_field` 用于批量修改父表字段，脚本见 [scripts/rebuild_character_memory.py](../../../../scripts/rebuild_character_memory.py)。

## 输入输出示例

适用 R4：

```text
输入：query_embedding=[...1024 维...]，query_text="2026年5月 川菜馆 老板娘 承诺"，
      date_from=None，date_to=None，final_limit=12
输出：[{"id": 31, "memory": "2026年5月...", "importance": 60, "event_date": "2026-05-12",
       "update_time": ..., "keywords": "川菜馆, 老板娘", "coarse_score": 0.72}, ...]
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 计算：[../processor/README.md](../processor/README.md) · 工具：[../../tools/memory_query/README.md](../../tools/memory_query/README.md)
- 分块：[../../utils/chunking/README.md](../../utils/chunking/README.md) · Reranker：[../../classes/reranker/README.md](../../classes/reranker/README.md) · 模型：[../../utils/models/README.md](../../utils/models/README.md)
- 依据：`agent/memory/store.py`；`tests/test_memory_service_postgres.py` 在 `MYBOT_MEMORY_DB_TESTS=1` 时覆盖真实 pgvector 检索；本次未执行测试。
