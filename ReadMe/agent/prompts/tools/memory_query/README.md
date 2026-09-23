# memory_query 提示词

## 职责与入口

- 所属类别：提示词模块（非图节点）。源码：[agent/prompts/tools/memory_query.py](../../../../../agent/prompts/tools/memory_query.py)。
- 注册与导出：`tools/__init__.py` 导入并列入 `__all__`（[agent/prompts/tools/__init__.py:7](../../../../../agent/prompts/tools/__init__.py)）；`agent/prompts/__init__.py` 再次导出，并提供 `get_prompt("memory_query")` 分发（[agent/prompts/__init__.py:38](../../../../../agent/prompts/__init__.py)）。
- 获取函数：`get_memory_query_prompt(language: str = "zh") -> str`（[memory_query.py:7](../../../../../agent/prompts/tools/memory_query.py)）。
- 消费方：`agent/memory/processor.py` 的 `process_memory_snapshot`（[agent/memory/processor.py:179](../../../../../agent/memory/processor.py)）。
- 触发时机：后台记忆总结任务开始时，且任务快照开启了记忆检索；与图内主模型自行调用的同名工具 `agent/tools/memory_query.py`（角色侧检索工具，以 ToolMessage 返回结果）不是同一条路径，本提示词只服务总结阶段的查询生成。

## 调用链总览

```text
（后台）process_memory_snapshot
    → 判断 memory_retrieval_enabled
    → get_prompt("memory_query")                    （本提示词，SystemMessage）
    → HumanMessage(world_state 文本 + 最近消息历史)
    → get_node_model("memory_query").ainvoke(...)
    → _parse_memory_queries(content)                （R1.1 解析 JSON 查询数组）
    → _retrieve_memories(store, queries, 12)        （R1.2 混合检索并按 id 去重）
    → related 记忆行 → 供 event_summary 步骤组装 <memories>
```

## 获取链

### B1. `get_memory_query_prompt`

- 定位与签名：`agent.prompts.tools.memory_query.get_memory_query_prompt(language: str = "zh") -> str`，同步函数，[源码 memory_query.py:7](../../../../../agent/prompts/tools/memory_query.py)。
- 调用方与条件：`process_memory_snapshot` 仅在 `memory_retrieval_enabled` 为真时调用（[agent/memory/processor.py:178](../../../../../agent/memory/processor.py)）；未传 `language`，取缺省 `"zh"`。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `language` | `str` | 默认 `"zh"` | 语言标识；`normalize_language` 后为 `"en"` 时返回英文分支 |

- 隐式输入：`normalize_language`（[agent/utils/language.py:4](../../../../../agent/utils/language.py)）。
- 功能与内部调用：归一语言；`"en"` 时调用 `_get_memory_query_prompt_en()`（[memory_query.py:13](../../../../../agent/prompts/tools/memory_query.py)，返回空字符串），否则调用 `_get_memory_query_prompt_zh()`（[memory_query.py:17](../../../../../agent/prompts/tools/memory_query.py)）。
- 输出：完整查询生成系统提示词字符串。
- 副作用与异常：无；不读文件、不调模型、不抛异常。

生成内容概要（不复制原文）：

| 部分 | 内容 |
| --- | --- |
| 角色定位 | 服务于记忆总结阶段的检索查询生成器；查询精准度决定记忆库是否重复存储、错误更新或误删 |
| 输入说明 | `<world_state>`（日期/星期/时间段/地点/天气）与 `<history_messages>`（带序号与说话者标识） |
| 任务步骤 | 识别有意义场景/已完成事件 → 判断涉及维度（事件、人物、承诺、地点、物品等）→ 每个维度生成一条日志体关键词查询 → 特定时间段事件附加日期范围 → 输出 JSON 数组 |
| 实体归一化 | 代词解析为具体姓名/身份，角色第一人称“我”保持不变；相对时间换算为绝对日期；地点保留层级；物品/事件去除主观评价 |
| 查询风格 | 结构为 `<时间锚点> <地点> <核心实体> <事件/事实关键词>`；空格分隔、无连接词、单条不超过 30 字符；禁止主观评价词与叙事性连接词；时间锚点优先绝对日期 |
| 日期字段 | 特定时间段查询附加 `date_from`/`date_to`（`yyyy-mm-dd`），按 world_state 当前日期换算并合理放宽；非时间特定查询省略；query 文本仍保留时间锚点 |
| 输出契约 | 只返回 JSON 数组，元素形如 `{"query": "...", "date_from": "...", "date_to": "..."}`，无解释、前缀或空行；附 4 组正反示例与 1 组完整示例 |

## 运行链

### R1. `process_memory_snapshot` 的检索分支（提示词消费）

- 定位与签名：`agent.memory.processor.process_memory_snapshot(payload: dict, memory_store) -> MemoryPlan`，异步；[agent/memory/processor.py:163](../../../../../agent/memory/processor.py)，检索分支见 [176-183 行](../../../../../agent/memory/processor.py)。
- 调用方与条件：后台 `MemoryWorker._process`（[agent/memory/worker.py:52](../../../../../agent/memory/worker.py)）；仅当 `payload["memory_retrieval_enabled"]`（缺省 `True`）为真时执行。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `payload["messages"]` | `list[dict]` | 入队快照 | 经 `messages_from_dict` 还原 | 取 `messages[max(0, start-6):]` 作为查询上下文，再截取末尾 120 条 |
| `payload["new_message_start"]` | `int` | 入队时计算 | 已校验范围 | 计算查询上下文起点 |
| `payload["world_state"]` | `dict \| None` | 入队快照 | `prepare_world_state` 转文本 | 拼接在历史之前 |
| `memory_store` | store 实例 | Worker | 必填 | 执行混合检索 |

- 隐式输入：提示词 B1 结果；模型 `get_node_model("memory_query")`；`format_history`（含工具消息格式化）；嵌入模型 `get_qwen_embedding_model`。
- 功能与内部调用：
  1. 组装 `[SystemMessage(get_prompt("memory_query")), HumanMessage(state_text + "\n" + format_history(query_context[-120:]))]`；
  2. `await get_node_model("memory_query").ainvoke(...)`；
  3. `_parse_memory_queries(response.content)`（R1.1）；
  4. `_retrieve_memories(memory_store, queries, final_limit=12)`（R1.2），得到 `related`；
  5. `known_ids = {row["id"] for row in related}`，随后与 `memories` 文本一起交给 `event_summary` 链路（见同级文档）。
- 输出与状态字段：

| 输出 | 类型 | 产生条件 | 含义与消费方 |
| --- | --- | --- | --- |
| `queries` | `list[dict]` | 解析成功 | 每项含 `query` 与可选 `date_from`/`date_to`；空列表时跳过检索 |
| `related` | `list[row]` | 检索成功 | 去重后的父记忆行；写入 `<memories>`，其 ID 集合限制 update/delete 范围 |

- 副作用：一次查询生成模型请求 + 每条查询一次嵌入与数据库混合检索。
- 异常与边界：查询模型调用异常未捕获，向上抛给 Worker 触发任务失败记录；解析失败返回空列表，不检索也不报错。
- 后续去向：`event_summary` 步骤；`related` 为空时 `<memories>` 为空块，总结模型只做新增与裁剪。

### R1.1 `_parse_memory_queries`

- 定位与签名：`agent.memory.processor._parse_memory_queries(content: str) -> list[dict]`，同步；[agent/memory/processor.py:19](../../../../../agent/memory/processor.py)。
- 输入：模型返回的文本。先 `strip_code_fence`，再 `json.loads`。
- 功能：接受新格式（对象数组）与旧格式（纯字符串数组，兼容为 `{"query": str}`）；丢弃非 dict 项与空 `query`；保留 `date_from`/`date_to` 原值。
- 输出：规范化查询列表；JSON 解析失败时记录日志并返回 `[]`。
- 边界：不校验日期格式，日期合法性由检索层处理。

### R1.2 `_retrieve_memories`

- 定位与签名：`agent.memory.processor._retrieve_memories(memory_store, queries: list[dict], final_limit: int) -> list`，异步；[agent/memory/processor.py:51](../../../../../agent/memory/processor.py)。
- 输入：查询列表与 `final_limit=12`。
- 功能：对每条查询调用 `encoder.encode(query["query"], prompt_name="query")`，再调用 `memory_store.search_hybrid(embedding, query_text, keyword_text, date_from, date_to, final_limit=12)`；按 `row["id"]` 去重合并。
- 输出：去重后的记忆行列表；`queries` 为空时直接返回 `[]`。
- 异常：嵌入或检索异常未捕获，向上抛给 Worker。

## 分支与异常链

- **关闭检索**：整个分支不执行，提示词不被获取；`known_ids` 为空，总结阶段只允许 insert/clean_history。
- **模型返回非法 JSON**：`_parse_memory_queries` 返回空列表，跳过检索，任务继续进入总结。
- **模型调用异常/检索异常**：任务失败并保留在队列，由 Worker 按重试上限处理。
- **EN 语言**：提示词为空串；当前调用方未传 `language`，不会进入该分支。
- **同名工具区分**：图内角色侧工具 `agent/tools/memory_query.py` 由主模型在对话中自行调用，其 description 与参数约束独立维护；两者共享“日志体关键词查询 + 混合检索”的形式，但调用者、输入来源与结果消费方式不同。

## 输入输出示例

适用 R1（示意，非真实内容）：

```text
SystemMessage：memory_query 提示词（中文）
HumanMessage：
{"date": "2026-09-23", "weekday": "星期三", "time_period": "evening", "location": "家中", "weather": "晴"}
index:0 User: 你还记得我们上个月约好要去看的那家店吗？
index:1 Character: 记得，当时说等它开业再去。
```

期望模型输出（经 R1.1 解析）：

```json
[{"query": "2026年8月 店铺 约定 我和[用户]", "date_from": "2026-08-01", "date_to": "2026-08-31"}]
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md)
- 相关提示词：[../event_summary/README.md](../event_summary/README.md)、[../event_judge/README.md](../event_judge/README.md)
- 消费源码：[agent/memory/processor.py](../../../../../agent/memory/processor.py)、[agent/memory/worker.py](../../../../../agent/memory/worker.py)；角色侧同名工具：[agent/tools/memory_query.py](../../../../../agent/tools/memory_query.py)
- 记忆服务：[../../../memory/README.md](../../../memory/README.md)
- 验证依据：静态阅读源码；本次未执行测试。
