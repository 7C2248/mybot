# chunking — 记忆文本分块、关键词与事件日期提取

## 职责与入口

- 所属类别：`agent/utils/` 支撑模块，**不是图节点**，无图注册名、无路由。
- 源码：[`agent/utils/chunking.py`](../../../../agent/utils/chunking.py)（模块 docstring：记忆写入和维护脚本共用的分块、关键词与日期提取）。
- 职责：把一条父记忆文本切成可检索的子块，并提取 `keywords` 与 `event_date` 元数据。主路径调用 LLM 做语义分块；LLM 超时、返回无效或重试耗尽时回退到纯正则函数切割。
- 公开入口：`chunk_memory`（异步入口）、`split_memory_text`、`extract_keywords`、`extract_event_date`（同步正则函数）。
- 调用方式：由记忆存储与维护脚本直接调用，不经过框架调度。

## 调用链总览

```text
R1 chunk_memory(text)  (async)
  ├─ R1.1 _chunk_with_llm(text)  (async)      ← LLM 语义分块（主路径）
  │    ├─ agent.utils.models.get_node_model("chunking")   [见 ../models/README.md]
  │    ├─ agent.prompts.tools.chunking.get_chunk_prompt() [见 ../../prompts/README.md]
  │    ├─ agent.utils.text.strip_code_fence               [见 ../text/README.md]
  │    ├─ json.loads + 结构校验（chunks 必须为非空数组）
  │    ├─ R1.1.1 split_memory_text(chunk)     ← 超过 200 字的 LLM chunk 二次函数截断
  │    └─ event_date 用 date.fromisoformat 校验
  └─ 返回 None（超时/异常重试耗尽）→ R2/R3/R4 正则回退
       ├─ R2 split_memory_text(text)
       ├─ R3 extract_keywords(text)
       └─ R4 extract_event_date(text)

调用方
  agent/memory/store.py prepare_memory            → await R1（自动提取仅在参数为 None 时生效）
  scripts/rebuild_character_memory.py             → await R1（默认）或 R2/R3/R4（--regex-only）
```

## 构建链

本模块无工厂与依赖注入，没有构建阶段；`_chunk_with_llm` 每次调用时按需导入模型与提示词（延迟导入，避免模块导入即加载模型）。

## 运行链

### R1. `chunk_memory`

- 定位与签名：`async def chunk_memory(memory_text: str) -> tuple[list[str], str | None, str | None]`，[`agent/utils/chunking.py:168`](../../../../agent/utils/chunking.py)，异步函数。
- 调用方与条件：
  - `agent/memory/store.py` 的 `prepare_memory` 无条件 `await chunk_memory(memory)`（[`agent/memory/store.py:149`](../../../../agent/memory/store.py)）；该函数由 `insert_memory` / `update_memory` 与后台 `processor.process_memory_snapshot`（经 `MemoryOperation.prepared`）触发。
  - `scripts/rebuild_character_memory.py` 的 `rebuild_chunks_for_memory`：非 `--regex-only` 模式调用（[`scripts/rebuild_character_memory.py:64`](../../../../scripts/rebuild_character_memory.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `memory_text` | `str` | 必填 | 一条父记忆的完整文本；来自工具参数、脚本读取的父表 `memory` 列 |

隐式输入：无（不读取全局配置、环境变量或数据库）。

功能与内部调用：

1. `await _chunk_with_llm(memory_text)`（R1.1）。
2. 若返回非 `None`，按 `(chunks, keywords, event_date)` 原样返回。
3. 若返回 `None`，调用 R2 `split_memory_text(memory_text)`、R3 `extract_keywords(memory_text)`、R4 `extract_event_date(memory_text)` 并作为回退结果返回。

输出：三元组 `(chunks, keywords, event_date)`；`keywords` 与 `event_date` 可为 `None`。消费方 `store.prepare_memory` 仅在显式参数为 `None` 时采用自动提取值（[`agent/memory/store.py:159`](../../../../agent/memory/store.py)）。

副作用：LLM 调用（网络请求）；无文件或数据库写入。

异常与边界：R1.1 内部异常全部被捕获并重试，最终返回 `None` 触发回退；本函数自身不抛异常（除非回退函数抛错，正则函数实际不抛）。`memory_text` 为空串时：LLM 路径可能失败后回退，`split_memory_text("")` 返回 `[""]`，`extract_keywords("")` 返回 `""`，`extract_event_date("")` 返回 `None`。

后续去向：返回值由调用方用于写入子表与父表元数据（见 [`../../memory/store/README.md`](../../memory/store/README.md)）。

### R1.1. `_chunk_with_llm`

- 定位与签名：`async def _chunk_with_llm(memory_text: str) -> dict | None`，[`agent/utils/chunking.py:96`](../../../../agent/utils/chunking.py)，异步内部函数。
- 调用方与条件：仅 R1 调用。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `memory_text` | `str` | 必填 | 完整父记忆文本，作为 `HumanMessage` 内容 |

隐式输入：

- 模块常量 `_LLM_CHUNK_TIMEOUT = 60`、`_LLM_CHUNK_MAX_RETRIES = 2`（即最多 3 次尝试）、`_CHUNK_MAX_CHARS = 200`（[`agent/utils/chunking.py:14`](../../../../agent/utils/chunking.py)、[`agent/utils/chunking.py:92`](../../../../agent/utils/chunking.py)）。
- 节点模型 `chunking`（`config/models.yaml` 中配置为 `deepseek-v4-pro`、`reasoning_effort: low`），经 `get_node_model` 获取。
- 提示词 `get_chunk_prompt()`（当前无语言参数，默认中文），要求模型输出 `{"event_date", "keywords", "chunks"}` JSON（[`agent/prompts/tools/chunking.py`](../../../../agent/prompts/tools/chunking.py)）。

功能与内部调用：

1. 延迟导入 `langchain_core.messages.{HumanMessage, SystemMessage}`、`get_node_model`、`get_chunk_prompt`；取模型与提示词。
2. 循环 `attempt in range(3)`：
   - `asyncio.wait_for(llm.ainvoke([SystemMessage(prompt), HumanMessage(text)]), timeout=60)`；
   - `content = (getattr(response, "content", "") or "").strip()`，再经 `strip_code_fence`（[`../text/README.md`](../text/README.md)）清洗 Markdown 包裹；
   - `json.loads`；要求结果为 dict 且 `chunks` 为非空 list，否则抛 `ValueError("LLM 返回缺少有效的 'chunks' 数组")`；
   - 逐个 `str(chunk).strip()`、跳过空串；长度超过 200 的 chunk 调 R2 `split_memory_text` 再截断；结果为空时抛 `ValueError("LLM 分块结果为空")`；
   - `event_date` 若存在，用 `date.fromisoformat(str(event_date)).isoformat()` 校验，非法则置 `None`（不因此失败）；
   - 返回 `{"chunks": final_chunks, "keywords": parsed.get("keywords") or None, "event_date": event_date}`。
3. 捕获 `asyncio.TimeoutError` 与一般 `Exception`，用 `logger.warning` 记录第几次尝试；未到最后一次时 `await asyncio.sleep(1.0)` 后重试。
4. 三次均失败后返回 `None`。

输出：成功为包含 `chunks`（`list[str]`）、`keywords`（`str | None`）、`event_date`（`str | None`，`yyyy-mm-dd`）的 dict；失败为 `None`。

副作用：对 `chunking` 节点模型发起最多 3 次网络调用（每次最长 60 秒）；无文件/数据库写入。

异常与边界：所有异常在函数内被捕获，不向上抛出；超时、JSON 解析失败、结构校验失败、事件日期非法均不阻断回退。`_LLM_CHUNK_MAX_RETRIES` 与 `_CHUNK_MAX_CHARS` 的语义不同：前者是接口重试上限，后者是单块字符上限。

后续去向：返回 R1 组装三元组。

### R2. `split_memory_text`

- 定位与签名：`split_memory_text(text: str, max_chars: int = 200, min_chars: int = 10) -> list[str]`，[`agent/utils/chunking.py:18`](../../../../agent/utils/chunking.py)，同步函数。
- 调用方与条件：
  - R1 回退路径；
  - R1.1 对超长 LLM chunk 的二次截断；
  - `scripts/rebuild_character_memory.py` 在 `--regex-only` 模式直接调用（[`scripts/rebuild_character_memory.py:59`](../../../../scripts/rebuild_character_memory.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `text` | `str` | 必填 | 待切割文本 |
| `max_chars` | `int` | 默认 200 | 单块最大字符数；超过该值的句子按固定长度硬切 |
| `min_chars` | `int` | 默认 10 | 缓冲块合并时的最小长度阈值（见下述边界行为） |

隐式输入：模块常量 `_SENT_SPLIT_RE = re.compile(r"[。！？；\n]")`、`_CHUNK_MAX_CHARS = 200`、`_CHUNK_MIN_CHARS = 10`（[`agent/utils/chunking.py:13`](../../../../agent/utils/chunking.py)）。

功能与内部调用：

1. 用 `_SENT_SPLIT_RE` 按 `。！？；` 和换行切分，逐段 `strip()`，跳过空段。
2. 对长度 `>= max_chars` 的段：先无条件落盘当前缓冲（即使缓冲短于 `min_chars`），再把该段按 `max_chars` 步长切片追加。
3. 对较短段：拼接候选 `buffer + "。" + part`（缓冲为空时直接用 `part`）；不超过 `max_chars` 就继续累积，超过则先按 `min_chars` 判断是否落盘旧缓冲，再以当前段开新缓冲。
4. 循环结束后，缓冲长度 `>= min_chars` 才追加。
5. 最终 `chunks` 为空时返回 `[text]`（保证至少一个元素）。

输出：`list[str]`；可能为空文本元素（当输入为空串且无任何切分时返回 `[""]`）。

副作用：无。

异常与边界：参数为 0 或负数时切片步长为 0 会抛 `ValueError`（调用方均使用默认值）；长度不足 `min_chars` 的尾段会被丢弃，仅当整段结果为空时以 `[text]` 兜底——这是当前实现的实际行为，调用方在空文本场景需自行校验（`store.prepare_memory` 会在编码后检查 `prepared` 为空并抛 `ValueError("记忆切块为空，不能提交缺少向量块的记录")`）。

后续去向：R1/R1.1 组装结果；脚本写入子表。

### R3. `extract_keywords`

- 定位与签名：`extract_keywords(text: str) -> str`，[`agent/utils/chunking.py:54`](../../../../agent/utils/chunking.py)，同步函数。
- 调用方与条件：R1 回退路径；`scripts/rebuild_character_memory.py` 的 `--regex-only` 模式（[`scripts/rebuild_character_memory.py:61`](../../../../scripts/rebuild_character_memory.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `text` | `str` | 必填 | 完整父记忆文本 |

隐式输入：无。

功能与内部调用（按顺序执行正则清洗与切分）：

1. 删除 `情绪：...`（到句号或换行）与 `关系变化：...` 片段；
2. 删除 `yyyy年M月d日?` 形式的日期；
3. 删除 `上午|下午|晚上|傍晚|中午|凌晨|早晨|早上` 时间泛称；
4. 按 `[，,。！？；;、\s]+` 切分，保留 `strip()` 后长度 `>= 2` 且非纯数字的片段；
5. 用 `", "` 连接前 8 个片段。

输出：逗号分隔的关键词字符串；无可用片段时返回 `""`。

副作用：无。

异常与边界：非字符串输入会在 `re.sub` 处抛 `TypeError`，调用方均传字符串。

后续去向：作为父表 `keywords` 列值（调用方在参数为 `None` 时采用）。

### R4. `extract_event_date`

- 定位与签名：`extract_event_date(text: str) -> str | None`，[`agent/utils/chunking.py:65`](../../../../agent/utils/chunking.py)，同步函数。
- 调用方与条件：R1 回退路径；`scripts/rebuild_character_memory.py` 的 `--regex-only` 模式（[`scripts/rebuild_character_memory.py:62`](../../../../scripts/rebuild_character_memory.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `text` | `str` | 必填 | 完整父记忆文本 |

隐式输入：无。

功能与内部调用（按优先级依次尝试三个正则，命中后构造 `datetime.date` 并返回 `isoformat()`）：

1. `(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?` → 完整年月日；
2. `(\d{4})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{1,2})` → `yyyy-mm-dd` / `yyyy/mm/dd`；
3. `(\d{4})\s*年\s*(\d{1,2})\s*月` → 只有年月时取该月 1 日。

输出：`yyyy-mm-dd` 字符串；无匹配或日期非法（如 `2026-02-31`）时返回 `None`。

副作用：无。

异常与边界：`date(...)` 抛出的 `ValueError` 被捕获后继续尝试下一格式；最终无有效日期返回 `None`。注意仅识别阿拉伯数字，且 `日` 可选。

后续去向：作为父表 `event_date` 列值（调用方在参数为 `None` 时采用）。

## 分支与异常链

| 条件 | 调用路径 | 输出 | 去向 |
| --- | --- | --- | --- |
| LLM 返回合法 JSON 且 `chunks` 非空 | R1 → R1.1 成功 | LLM 分块 + LLM 提取的 `keywords`/`event_date` | R1 返回 |
| LLM chunk 超过 200 字 | R1.1 内部调 R2 | 二次截断后的块 | R1.1 继续组装 |
| `event_date` 非法 | R1.1 置 `None` | 仍返回合法 `chunks`/`keywords` | R1 返回 |
| 超时/异常 3 次尝试耗尽 | R1.1 返回 `None` | R2/R3/R4 正则结果 | R1 返回 |
| `--regex-only` | 脚本直接调 R2/R3/R4 | 不访问 LLM | 脚本写子表与父表元数据 |

## 输入输出示例

示例 1（R2，超长单句硬切，与 `tests/test_module_layout.py:159` 的断言一致）：

```text
输入: "长" * 450（无标点）
输出: ["长"*200, "长"*200, "长"*50]
```

示例 2（R2，短文本合并为一块）：

```text
输入: "2026年3月15日，雨天，咖啡馆。我第一次见到他。"
输出: ["2026年3月15日，雨天，咖啡馆。我第一次见到他"]   ← 各段均短于 200，合并后长度 >= 10
```

示例 3（R3/R4）：

```text
输入: "2026年3月15日 下午，雨天，咖啡馆。我第一次见到他。情绪：紧张、期待。"
R3 输出: "雨天, 咖啡馆, 我第一次见到他"
R4 输出: "2026-03-15"
```

示例 4（R1.1 LLM 返回，示意字段形状）：

```json
{
  "chunks": ["2026年3月15日下午，雨天，咖啡馆", "我第一次见到他", "情绪：紧张、期待"],
  "keywords": "咖啡馆, 初次见面, 雨天",
  "event_date": "2026-03-15"
}
```

示例 5（R1 回退，`_chunk_with_llm` 返回 `None`，`tests/test_module_layout.py:178` 的同构场景）：

```text
输入: "2026年9月12日去公园。"
输出: (["2026年9月12日去公园"], "去公园", "2026-09-12")
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)（agent/utils 总览）
- 相关模块：[`../models/README.md`](../models/README.md)（`get_node_model`）、[`../text/README.md`](../text/README.md)（`strip_code_fence`）、[`../../prompts/README.md`](../../prompts/README.md)（`get_chunk_prompt`）、[`../../memory/store/README.md`](../../memory/store/README.md)（消费方）
- 调用方源码：[`agent/memory/store.py:149`](../../../../agent/memory/store.py)、[`scripts/rebuild_character_memory.py:59`](../../../../scripts/rebuild_character_memory.py)
- 已有测试覆盖（本次未执行）：`tests/test_module_layout.py:157-159`（`extract_event_date` 合法/非法日期、`split_memory_text` 长度上限）、`tests/test_module_layout.py:178-180`（`_chunk_with_llm` 返回 `None` 时的回退）
- 验证情况：本页为静态阅读源码所得；`_chunk_with_llm` 的成功分支（真实模型 JSON 输出）没有离线测试覆盖，文中示例为按代码契约构造的示意数据。
