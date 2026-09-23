# MemorySearchQuery / MemoryQueryInput / MemoryHit / MemorySearchResult（记忆检索工具输入输出协议）

## 职责与入口

- 所属类别：工具输入输出协议（`agent/classes/memory.py`），不是图节点，没有工厂、路由或可执行入口。
- 源码：[`memory.py`](../../../../agent/classes/memory.py)
- 主要使用方：[`agent/tools/memory_query.py`](../../../../agent/tools/memory_query.py) 的 `CharacterMemoryQueryTool`——`args_schema = MemoryQueryInput`，`_arun` 内构造 `MemoryQueryInput`、`MemoryHit`、`MemorySearchResult` 并返回 JSON 字符串。
- 次要使用方：[`server/services/agent.py`](../../../../server/services/agent.py) 在 `tools` 节点输出中解析 `MemorySearchResult` 并发布 `memory.retrieved` 事件；测试导入 `MemoryQueryInput`。

## 定义

### `MemorySearchQuery`（继承 `_StrictModel`，`extra="forbid"`）

| 字段 | 类型 | 默认值 | 语义与约束 |
| --- | --- | --- | --- |
| `query` | `str` | 无（必填，`min_length=1`） | 日志体关键词检索序列：`<时间锚点> <地点> <核心实体> <事件/事实关键词>`，空格分隔、不超过 30 字符、每次只聚焦一个检索维度；代词解析为具体实体，相对时间按已知锚点换算为绝对日期；未知要素省略 |
| `date_from` | `date \| None` | `None` | 起始日期；仅用于有明确时间依据的特定时间段事件，与 `date_to` 成对提供 |
| `date_to` | `date \| None` | `None` | 结束日期；不得早于 `date_from` |

校验器：

- `normalize_bare_query`（`mode="before"`）：兼容模型把 `query` 直接输出为字符串的退化形式，`"关键词"` 会被归一为 `{"query": "关键词"}`。
- `validate_query`（`mode="after"`）：`query.strip()` 后不得为空；`date_from` 与 `date_to` 必须同时提供或同时省略；起始日期不得晚于结束日期。

### `MemoryQueryInput(MemorySearchQuery)`

在继承上述字段与校验的基础上追加：

| 字段 | 类型 | 默认值 | 约束 | 语义 |
| --- | --- | --- | --- | --- |
| `limit` | `int` | `12` | `strict=True`、`ge=1`、`le=20` | 返回记忆条数上限，一般保持默认 |

### `MemoryHit`

| 字段 | 类型 | 默认值 | 约束 | 语义 |
| --- | --- | --- | --- | --- |
| `memory_id` | `int` | 无（必填） | `strict=True`、`gt=0` | 父记忆表主键 |
| `text` | `str` | 无（必填） | `min_length=1` | 父记忆原文 |
| `event_date` | `str \| None` | `None` | 由数据库 `DATE` 字符串化 | 记忆对应事件日期 |
| `update_time` | `str \| None` | `None` | 由数据库时间戳字符串化 | 记忆最后更新时间 |

### `MemorySearchResult`

| 字段 | 类型 | 默认值 | 语义 |
| --- | --- | --- | --- |
| `status` | `Literal["ok","empty","error"]` | 无（必填） | 检索结论 |
| `hits` | `list[MemoryHit]` | 无（必填） | 命中的父记忆；仅包含父记忆原文，不含子块与分数 |
| `error` | `str \| None` | `None` | 失败类型名（如 `RuntimeError`），仅 `error` 状态填写 |

校验器 `validate_status` 保证：

- `ok` 必须有命中；
- `empty` / `error` 不得包含命中；
- `error` 必须包含错误类型；成功或空结果不得包含错误；
- 命中列表内 `memory_id` 不得重复。

## 构造或校验

1. LangChain 依据 `args_schema` 先校验工具调用参数，再进入 `_arun(query, date_from=None, date_to=None, limit=_DEFAULT_FINAL_LIMIT)`；同步入口 `_run` 直接 `raise NotImplementedError("memory_query 仅支持异步调用")`。
2. `_arun` 内再次 `MemoryQueryInput(...)` 构造并 `model_dump(mode="json")`，把 `date` 字段转换为 `yyyy-mm-dd` 字符串，供 `search_hybrid` 的时间过滤下推使用。
3. 检索行由 `MemoryHit(...)` 构造：`memory_id=row["id"]`、`text=row["memory"]`，日期与时间做字符串化；以 `memory_id` 为键去重后再组装。
4. 结果状态：有命中为 `ok`，无命中为 `empty`；任何异常（含 `memory_store is None`）被捕获后返回 `status="error"`、`hits=[]`、`error=type(exc).__name__`，不向上抛出。

## 生产方

| 生产位置 | 产物 | 触发条件 |
| --- | --- | --- |
| `CharacterMemoryQueryTool.args_schema` | `MemoryQueryInput` | 工具类定义时声明，供 LangChain 生成参数校验 |
| `_arun`（[`agent/tools/memory_query.py`](../../../../agent/tools/memory_query.py)） | `MemoryQueryInput`、`MemoryHit`、`MemorySearchResult` | 主模型发起 `memory_query` 工具调用且 `memory_store` 可用时 |
| 工具失败路径 | `MemorySearchResult(status="error", ...)` | `memory_store is None`、编码模型或 `search_hybrid` 抛异常 |

## 消费方

| 消费位置 | 读取内容 | 用途 |
| --- | --- | --- |
| `_arun` 返回 | `MemorySearchResult.model_dump_json()` | 作为工具返回值进入 `ToolMessage.content`，被主模型与后续轮次直接读取 |
| `server/services/agent.py` | `MemorySearchResult.model_validate_json(message.content)` 的 `status`、`hits` | `status in ("ok","empty")` 时发布 `memory.retrieved` 事件，`hits` 映射为 `{"id","memory","event_date","update_time"}`；解析失败则跳过 |
| 测试 | `MemoryQueryInput` | `tests/test_reply_memory.py` 验证查询协议；`tests/test_rp_pipeline.py` 以真实工具驱动 draft → tools 循环 |

## 输入输出示例

模型发起的工具调用参数：

```json
{"query": "2026年11月20日 公园 约定", "date_from": "2026-11-19", "date_to": "2026-11-21"}
```

成功返回（`limit` 缺省为 12）：

```json
{"status": "ok", "hits": [{"memory_id": 42, "text": "2026年11月20日，两人约定周六去公园。",
                           "event_date": "2026-11-20", "update_time": "2026-11-20 21:03:11"}],
 "error": null}
```

空结果与失败：

```json
{"status": "empty", "hits": [], "error": null}
{"status": "error", "hits": [], "error": "RuntimeError"}
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)
- 兄弟协议：[`../state/README.md`](../state/README.md)、[`../memory_job/README.md`](../memory_job/README.md)
- 生产工具：[`../../tools/README.md`](../../tools/README.md)、[`../../tools/memory_query/README.md`](../../tools/memory_query/README.md)
- 实现依据：[`agent/tools/memory_query.py`](../../../../agent/tools/memory_query.py)、[`server/services/agent.py`](../../../../server/services/agent.py)、[`agent/memory/store.py`](../../../../agent/memory/store.py) 的 `search_hybrid` 返回行结构
- 测试覆盖（静态阅读交叉核对，未在本页重新执行）：`tests/test_reply_memory.py`（`MemoryQueryInput`、工具消息持久化）、`tests/test_rp_pipeline.py`（真实工具与 tools 循环）
- 未验证项：`search_hybrid` 的返回行由数据库查询决定，本协议仅约束工具层可见字段。
