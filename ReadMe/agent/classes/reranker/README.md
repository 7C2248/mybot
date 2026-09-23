# CrossEncoderReranker（LangChain 文档重排器）

## 职责与入口

- 所属类别：检索组件（继承 LangChain `BaseDocumentCompressor`），不是图节点，没有图注册名。
- 源码：[`reranker.py`](../../../../agent/classes/reranker.py)
- 构造方：[`agent/utils/models.py`](../../../../agent/utils/models.py) 的 `get_reranker_model`（主链）与 `load_reranker`（独立入口，当前未被其他模块引用）。
- 调用方：[`agent/memory/store.py`](../../../../agent/memory/store.py) 的 `AsyncPostgresCharacterMemoryStore._rerank`，由 `search_hybrid` 第 5 步触发，调用 `acompress_documents`；`compress_documents` 是同步实现，仅由 `acompress_documents` 经线程转发。

## 定义

```python
class CrossEncoderReranker(BaseDocumentCompressor):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    model: HuggingFaceCrossEncoder
    top_k: int = 4
```

| 字段 | 类型 | 默认值 | 语义与约束 |
| --- | --- | --- | --- |
| `model` | `HuggingFaceCrossEncoder` | 无默认值（必填） | 交叉编码器；`arbitrary_types_allowed=True` 允许 Pydantic 持有该第三方类型 |
| `top_k` | `int` | `4` | 最多保留的文档数；实际链中由调用方按 `final_limit` 覆盖 |

## 构造或校验

| 构造位置 | 输入 | 行为 |
| --- | --- | --- |
| `get_reranker_model(top_k: int = 15)`（[`agent/utils/models.py`](../../../../agent/utils/models.py)） | `top_k`，默认 15；`@lru_cache(maxsize=8)` 按 `top_k` 缓存包装器实例 | `CrossEncoderReranker(model=_get_reranker_cross_encoder(), top_k=top_k)`；底层权重由 `_get_reranker_cross_encoder`（`@lru_cache(maxsize=1)`）共享，模型路径为 `config.config.BGEV2M3_RERANKER_PATH`，不存在时抛 `FileNotFoundError`，构造时 `model_kwargs={"device": "cuda"}` |
| `load_reranker(model_path=BGEV2M3_RERANKER_PATH, top_k: int = 4)` | 模型路径与 `top_k` | 每次都新建 `HuggingFaceCrossEncoder(model_name=model_path, model_kwargs={"device": "cuda", "torch_dtype": torch.float16})` 再包装；路径缺失抛 `FileNotFoundError`。当前全仓库未发现调用点，属于独立/备用入口，不参与现行检索链 |
| `AsyncPostgresCharacterMemoryStore._get_reranker(top_k=50)` | `top_k` | 实例级薄缓存：缓存为空或 `top_k` 不同才调用 `get_reranker_model(top_k=top_k)` 重建；实际由 `_rerank` 传入 `final_limit` |

构造阶段只做类型与路径校验，不对文档打分；`top_k` 的合法性由 Pydantic `int` 字段约束。

## 调用链

### R1. `compress_documents`（同步打分与截断）

- 定位与签名：`CrossEncoderReranker.compress_documents(self, documents: Sequence[Document], query: str, callbacks: Optional[Callbacks] = None) -> Sequence[Document]`，同步方法；源码 [`reranker.py`](../../../../agent/classes/reranker.py)。
- 调用方与条件：`acompress_documents` 在线程中调用；当前链路只在 `store._rerank` 触发，条件为 `use_reranker=True`、`query_text` 非空且粗排候选多于 1 条。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `documents` | `Sequence[Document]` | 必填 | `store._rerank` 用粗排候选构造，`page_content=r["memory"]`、`metadata={"idx": i}` |
| `query` | `str` | 必填 | `search_hybrid(query_text=...)` 传入的原始查询文本 |
| `callbacks` | `Optional[Callbacks]` | `None` | 兼容基类签名，本实现未使用 |

功能与内部调用：

1. `documents` 长度为 0 时直接返回 `[]`。
2. `scores = list(self.model.score([(query, doc.page_content) for doc in documents]))`：对每个 `(query, 文档正文)` 对调用交叉编码器打分。
3. `sorted(zip(documents, scores), key=lambda x: x[1], reverse=True)`：按分数降序排列。
4. 返回前 `self.top_k` 个文档。

| 输出 | 类型 | 产生条件 | 含义与消费方 |
| --- | --- | --- | --- |
| 排序后的文档 | `Sequence[Document]` | 非空输入 | `store._rerank` 依据 `metadata["idx"]` 重建父记忆行 |
| `[]` | `list` | 输入为空 | 表示无可重排文档 |

异常与边界：`self.model.score` 的异常不捕获，向上抛给 `acompress_documents` 调用方；`store._rerank` 捕获异常后回退粗排结果。

### R2. `acompress_documents`（异步包装）

- 定位与签名：`async def acompress_documents(self, documents, query, callbacks=None)`；源码 [`reranker.py`](../../../../agent/classes/reranker.py)。
- 调用方与条件：`store._rerank` 中 `await reranker.acompress_documents(docs, query_text)`。
- 输入：与 R1 相同，`callbacks` 原样转发。
- 功能：函数内导入 `asyncio`，执行 `await asyncio.to_thread(self.compress_documents, documents, query, callbacks)`，把同步打分放到线程，避免阻塞事件循环。
- 输出：与 R1 相同的文档序列；异常在线程中抛出后原样向上传播。
- 副作用：无文件或数据库写入；仅模型推理。

## 生产方与消费方

- 生产方（构造）：`agent/utils/models.py` 的 `get_reranker_model`（按 `top_k` 缓存，权重共享）、`load_reranker`（独立备用）。
- 消费方（调用）：
  - `store.search_hybrid` 第 5 步：`if use_reranker and query_text and len(coarse_rows) > 1: return await self._rerank(coarse_rows, query_text, rerank_top_k=rerank_top_k, final_limit=final_limit)`；
  - `store._rerank`：候选截断为 `rows[:rerank_top_k]`，构造 `Document` 列表并携带下标，`_get_reranker(top_k=final_limit)` 后调用 `acompress_documents`；
  - 结果重建：按 `d.metadata["idx"]` 取回候选；数量不足 `final_limit` 时按粗排顺序补齐；异常时 `logger.warning("精排失败，回退粗排结果: ...")` 并返回 `candidates[:final_limit]`。

## 输入输出示例

`store._rerank` 的输入与输出（虚构数据）：

```python
# 输入
rows = [{"id": 7, "memory": "周六去公园的约定", "coarse_score": 0.83, ...},
        {"id": 9, "memory": "关于天气的闲聊", "coarse_score": 0.51, ...}]
query_text = "2026年11月20日 公园 约定"

# 调用
reranked = await reranker.acompress_documents(
    [Document(page_content="周六去公园的约定", metadata={"idx": 0}),
     Document(page_content="关于天气的闲聊", metadata={"idx": 1})],
    "2026年11月20日 公园 约定")

# 输出（top_k=final_limit 时按分数降序）
[Document(page_content="周六去公园的约定", metadata={"idx": 0})]
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)
- 兄弟协议：[`../memory/README.md`](../memory/README.md)、[`../memory_job/README.md`](../memory_job/README.md)
- 协作模块：[`../../memory/README.md`](../../memory/README.md)、[`../../utils/README.md`](../../utils/README.md)
- 实现依据：[`agent/classes/reranker.py`](../../../../agent/classes/reranker.py)、[`agent/utils/models.py`](../../../../agent/utils/models.py)、[`agent/memory/store.py`](../../../../agent/memory/store.py)（`_get_reranker`、`search_hybrid`、`_rerank`）
- 测试覆盖：当前 `tests/` 未发现直接覆盖 `CrossEncoderReranker` 或 `store._rerank` 的用例，本页依据源码静态阅读；`load_reranker` 未发现调用点，标注为独立入口。
