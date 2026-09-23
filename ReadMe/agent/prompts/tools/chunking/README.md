# chunking 提示词

## 职责与入口

- 所属类别：提示词模块（非图节点）。源码：[agent/prompts/tools/chunking.py](../../../../../agent/prompts/tools/chunking.py)。
- 注册与导出：`tools/__init__.py` 导入并列入 `__all__`（[agent/prompts/tools/__init__.py:4](../../../../../agent/prompts/tools/__init__.py)）；`agent/prompts/__init__.py` 再次导出，但 `get_prompt` **没有** `chunking`/`keyword_extract` 类型键。
- 获取函数：
  - `get_chunk_prompt(language: str = "zh") -> str`（[chunking.py:11](../../../../../agent/prompts/tools/chunking.py)），主链使用；
  - `get_keyword_extract_prompt(language: str = "zh") -> str`（[chunking.py:108](../../../../../agent/prompts/tools/chunking.py)），**当前未接入主链**（无调用方）。
- 消费方：`agent/utils/chunking.py` 的 `_chunk_with_llm` 在函数内直接 `from agent.prompts.tools.chunking import get_chunk_prompt`（[agent/utils/chunking.py:104](../../../../../agent/utils/chunking.py)），未经过统一入口。
- 触发时机：记忆文本需要切块时，即 `chunk_memory` 被调用时。调用发生在两个上下文：常规 CRUD 的 `store.prepare_memory`，以及后台 Worker 的记忆总结计划计算（[agent/memory/processor.py:237](../../../../../agent/memory/processor.py)）。

## 调用链总览

```text
store.prepare_memory / process_memory_snapshot
  → chunk_memory(memory_text)
      → _chunk_with_llm(memory_text)         （本提示词的消费点）
          → get_chunk_prompt()               （zh，无参数）
          → get_node_model("chunking").ainvoke([SystemMessage(prompt), HumanMessage(text)])
          → 解析 JSON（chunks/keywords/event_date），校验失败重试
          ├─ 成功 → 返回 dict
          └─ 3 次均失败 → 返回 None
      → 回退路径（仅 None 时）：split_memory_text + extract_keywords + extract_event_date（正则，不经提示词）
```

`get_keyword_extract_prompt` 不在上述任一链路中；分块失败时的关键词回退使用正则函数 `extract_keywords`（[agent/utils/chunking.py:54](../../../../../agent/utils/chunking.py)）。

## 获取链

### B1. `get_chunk_prompt`

- 定位与签名：`agent.prompts.tools.chunking.get_chunk_prompt(language: str = "zh") -> str`，同步函数，[源码 chunking.py:11](../../../../../agent/prompts/tools/chunking.py)。
- 调用方与条件：`_chunk_with_llm` 每次尝试前调用一次（[agent/utils/chunking.py:107](../../../../../agent/utils/chunking.py)）；调用时不传参数，`language` 取默认 `"zh"`。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `language` | `str` | 默认 `"zh"` | 语言标识；`normalize_language` 后为 `"en"` 时返回英文分支 |

- 隐式输入：`normalize_language`（[agent/utils/language.py:4](../../../../../agent/utils/language.py)）。
- 功能与内部调用：归一语言；`"en"` 时调用 `_get_chunk_prompt_en()`（[chunking.py:104](../../../../../agent/prompts/tools/chunking.py)，返回空字符串），否则调用 `_get_chunk_prompt_zh()`（[chunking.py:17](../../../../../agent/prompts/tools/chunking.py)）。
- 输出：完整分块系统提示词字符串。当前调用方不传 `language`，实际生效的是中文提示词；EN 分支为空串，且无调用方传入 `"en"`。
- 副作用与异常：无；不读文件、不调模型、不抛异常。

### B2. `get_keyword_extract_prompt`（当前未接入主链）

- 定位与签名：同文件 [chunking.py:108](../../../../../agent/prompts/tools/chunking.py)，`language` 默认 `"zh"`；zh 分支见 [chunking.py:115](../../../../../agent/prompts/tools/chunking.py)，EN 分支返回空串。
- 状态说明：该 getter 被逐级导出，但仓库内没有任何调用方；`_chunk_with_llm` 失败后的关键词回退走正则 `extract_keywords`，不会使用本提示词。保留为备用/历史能力，接入前不属于当前调用链。

生成内容概要（不复制原文）：

| 提示词 | 内容 |
| --- | --- |
| `_get_chunk_prompt_zh`（分块主提示词） | 输入为日志体记忆（`<时间锚点> <地点> <核心事实>。情绪：…。关系变化：…。`）；任务一按结构分离四类内容（时间地点锚点、事件内容、情绪标签、关系变化标签），事件内容超过 80 字才继续拆分，其余类别不受字数限制；任务二提取 3~8 个核心关键词（专有名词/物品/事件动词，排除情绪词与泛化时间词）；任务三从时间锚点提取 `yyyy-mm-dd` 事件日期（仅年月取当月 1 日，仅年取 1 月 1 日，无时间返回 `null`）；严格输出 `{"event_date","keywords","chunks"}` JSON，无代码块与解释 |
| `_get_keyword_extract_prompt_zh`（未接入） | 仅提取 3~8 个关键词并只输出关键词字符串；供分块失败时的备用路径设计，当前未调用 |

## 运行链

### R1. `_chunk_with_llm`（提示词消费）

- 定位与签名：`agent.utils.chunking._chunk_with_llm(memory_text: str) -> dict | None`，异步；[agent/utils/chunking.py:96](../../../../../agent/utils/chunking.py)。
- 调用方与条件：`chunk_memory`（[agent/utils/chunking.py:168](../../../../../agent/utils/chunking.py)）每次调用时执行；`chunk_memory` 由 `store.prepare_memory` 调用（[agent/memory/store.py:149](../../../../../agent/memory/store.py)），后者被常规写入与后台记忆计划复用。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `memory_text` | `str` | `chunk_memory` 入参，来自待写入记忆文本 | 必填；空文本也会送入，由模型与后续校验处理 | 作为 `HumanMessage` 内容，不插入提示词模板 |

- 隐式输入：闭包外常量 `_LLM_CHUNK_TIMEOUT = 60`、`_LLM_CHUNK_MAX_RETRIES = 2`、`_CHUNK_MAX_CHARS = 200`；模型 `get_node_model("chunking")`；提示词 B1 结果。
- 功能与内部调用：
  1. 取模型与提示词，组装 `[SystemMessage(prompt), HumanMessage(memory_text)]`；
  2. `asyncio.wait_for(llm.ainvoke(...), timeout=60)`，失败/超时按 1 秒间隔重试，最多 3 次尝试；
  3. 用 `strip_code_fence` 清洗后 `json.loads`；要求顶层为 dict 且 `chunks` 为非空数组，否则抛错进入重试；
  4. 逐块 `strip` 并丢弃空块；超过 200 字的块交给 `split_memory_text` 再次截断；
  5. 校验 `event_date` 可用 `date.fromisoformat` 解析，否则置 `None`；
  6. 返回 `{"chunks", "keywords", "event_date"}`；`keywords` 为空时返回 `None`。
- 输出与状态字段：

| 输出 | 类型 | 产生条件 | 含义与消费方 |
| --- | --- | --- | --- |
| `{"chunks","keywords","event_date"}` | `dict` | 任意一次尝试成功且校验通过 | `chunk_memory` 直接返回三元组；`prepare_memory` 用于向量编码与入库字段 |
| `None` | `None` | 3 次尝试均失败或校验失败 | `chunk_memory` 回退到函数切割路径 |

- 副作用：模型请求（可能 3 次）；日志记录失败原因。
- 异常与边界：`asyncio.TimeoutError` 与一般异常都在函数内捕获并记日志，不向上抛；重试间隔 1 秒。
- 后续去向：成功结果进入 `prepare_memory` 的 `PreparedMemory`（自动关键词/日期仅在调用方未显式传入时采用）；失败回退路径见下。

### R2. `chunk_memory` 回退路径（不经提示词）

- 定位：`agent.utils.chunking.chunk_memory`（[agent/utils/chunking.py:168](../../../../../agent/utils/chunking.py)）。
- 条件：`_chunk_with_llm` 返回 `None`。
- 行为：调用 `split_memory_text`（标点断句 + 超长截断 + 过短合并，[agent/utils/chunking.py:18](../../../../../agent/utils/chunking.py)）、`extract_keywords`（正则关键词，[agent/utils/chunking.py:54](../../../../../agent/utils/chunking.py)）、`extract_event_date`（正则日期，[agent/utils/chunking.py:65](../../../../../agent/utils/chunking.py)）。
- 输出：三元组 `(chunks, keywords, event_date)`；`keywords`/`event_date` 可能为 `None`。
- 边界：`split_memory_text` 在无有效分块时返回整段文本；回退路径不读取任何提示词。

## 分支与异常链

- **EN 语言**：`get_chunk_prompt` 的 EN 分支为空串；当前无调用方传 `language`，实际不会走到。
- **JSON 解析失败/结构缺失/分块为空**：抛 `ValueError` 被捕获，进入下一次尝试；3 次后返回 `None` 并回退 R2。
- **超长块**：模型已按提示词限制事件内容 80 字，但消费方仍以 200 字为硬上限再次截断，作为防御性处理。
- **`get_keyword_extract_prompt`**：注册与导出存在，但无任何调用方，不属于当前主链或回退链。

## 输入输出示例

适用 R1（示意，非真实用户内容）：

```text
HumanMessage：
2024年3月15日 下午，雨天，咖啡馆。我第一次见到[用户名]。情绪：紧张、期待。关系变化：初次接触。
```

期望模型输出（经消费方校验后进入 `chunks`）：

```json
{
  "event_date": "2024-03-15",
  "keywords": "咖啡馆, 见面, 初次接触, 用户名",
  "chunks": [
    "2024年3月15日下午，雨天，咖啡馆",
    "我第一次见到[用户名]",
    "情绪：紧张、期待",
    "关系变化：初次接触"
  ]
}
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md)
- 同级提示词：[../check/README.md](../check/README.md)
- 消费源码：[agent/utils/chunking.py](../../../../../agent/utils/chunking.py)、[agent/memory/store.py](../../../../../agent/memory/store.py)、[agent/memory/processor.py](../../../../../agent/memory/processor.py)
- 验证依据：静态阅读源码；本次未运行测试。“未接入”结论基于全仓库检索不到调用方。
