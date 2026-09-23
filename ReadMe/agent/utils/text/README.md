# text — 消息内容块、时间戳与代码围栏清洗

## 职责与入口

- 所属类别：`agent/utils/` 支撑模块，**不是图节点**，无图注册名、无路由。
- 源码：[`agent/utils/text.py`](../../../../agent/utils/text.py)（模块 docstring：模型响应中共用的内容块、时间戳和 Markdown 容器处理）。
- 职责：从 LangChain 消息的 `content` 中提取纯文本、移除程序注入的 `<timestamp>` 标签、剥离模型输出外层的 Markdown 代码围栏。
- 公开入口：`content_text`、`strip_timestamps`、`strip_code_fence`，均为单表达式级同步函数，无内部辅助函数。
- 调用方式：同步函数，由回复、检查、状态、TTS 节点与后台处理器在解析模型输出时直接调用。

## 调用链总览

```text
R1 content_text(content)          ← 提取消息文本块（忽略图片等非文本内容）
R2 strip_timestamps(text)         ← 移除 <timestamp>...</timestamp>\n
R3 strip_code_fence(text)         ← 移除 ``` 代码围栏

典型组合（按调用方实际顺序）
  draft:            R1 → R2（再进入节点内部 _extract_reply）
  check:            R1 → R2（提取最近用户输入）
  participant_state: R1 → R2 → R3（再交给 ParticipantStateUpdate 校验）
  tts:              R2（_parse_reply）、R3（_parse_plans）
  chunking:         R3（_chunk_with_llm 清洗 LLM JSON）
  processor:        R3（_parse_memory_queries 清洗 LLM JSON）
```

## 构建链

无工厂、无缓存、无依赖注入；仅依赖标准库 `re`。

## 运行链

### R1. `content_text`

- 定位与签名：`content_text(content: str | list | None) -> str`，[`agent/utils/text.py:6`](../../../../agent/utils/text.py)，同步函数。
- 调用方与条件：
  - `agent/node/draft.py`：读取模型响应的正文（[`agent/node/draft.py:91`](../../../../agent/node/draft.py)）；
  - `agent/node/participant_state.py`：取待更新消息的文本、取模型响应文本（[`agent/node/participant_state.py:38`](../../../../agent/node/participant_state.py)、[`agent/node/participant_state.py:78`](../../../../agent/node/participant_state.py)）；
  - `agent/node/check.py`：`_latest_user_input` 取最近用户输入（[`agent/node/check.py:145`](../../../../agent/node/check.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `content` | `str | list | None` | 必填 | LangChain 消息的 `content`；列表形式常见于多模态响应 |

隐式输入：无。

功能与内部调用：

1. 若 `content` 是 `list`，遍历元素：字符串元素原样拼接；dict 元素取 `block.get("text", "")`；其他类型忽略；
2. 其他情况返回 `content or ""`（`None` 与空串都得到 `""`）。

输出：`str`。副作用：无。异常与边界：dict 元素缺少 `text` 键时贡献空串；`content` 为数字等其他类型时原样返回非字符串（调用方通常直接 `.strip()`，会抛 `AttributeError`，当前消息内容不会是这种类型）。

后续去向：返回调用方后常继续 R2、`.strip()` 或节点内部解析。

### R2. `strip_timestamps`

- 定位与签名：`strip_timestamps(text: str) -> str`，[`agent/utils/text.py:16`](../../../../agent/utils/text.py)，同步函数。
- 调用方与条件：
  - `agent/node/draft.py`：对提取出的候选正文清洗（[`agent/node/draft.py:97`](../../../../agent/node/draft.py)）；
  - `agent/node/participant_state.py`：取消息文本与模型响应时清洗（[`agent/node/participant_state.py:38`](../../../../agent/node/participant_state.py)、[`agent/node/participant_state.py:78`](../../../../agent/node/participant_state.py)）；
  - `agent/node/check.py`：提取最近用户输入（[`agent/node/check.py:145`](../../../../agent/node/check.py)）；
  - `agent/node/tts.py`：`_parse_reply` 在分段前清洗（[`agent/node/tts.py:38`](../../../../agent/node/tts.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `text` | `str` | 必填 | 含程序注入时间戳的文本；用户消息格式为 `<timestamp>...</timestamp>\n正文` |

隐式输入：无。

功能与内部调用：`re.sub(r"<timestamp>.*?</timestamp>\n?", "", text, flags=re.DOTALL)`——非贪婪匹配成对标签，连同紧随其后的一个换行一起删除；多处时间戳全部删除。

输出：清洗后的文本；没有时间戳时原样返回。副作用：无。异常与边界：只有开标签或只有闭标签时不匹配，保持原文；`<timestamp>` 内可跨行（`DOTALL`）。

后续去向：返回调用方继续解析。

### R3. `strip_code_fence`

- 定位与签名：`strip_code_fence(text: str) -> str`，[`agent/utils/text.py:21`](../../../../agent/utils/text.py)，同步函数。
- 调用方与条件：
  - `agent/node/participant_state.py`：模型 JSON 校验前（[`agent/node/participant_state.py:79`](../../../../agent/node/participant_state.py)）；
  - `agent/node/tts.py`：`_parse_plans` 解析语气指令 JSON 前（[`agent/node/tts.py:84`](../../../../agent/node/tts.py)）；
  - `agent/utils/chunking.py`：`_chunk_with_llm` 解析分块 JSON 前（[`agent/utils/chunking.py:121`](../../../../agent/utils/chunking.py)）；
  - `agent/memory/processor.py`：`_parse_memory_queries` 解析检索语句 JSON 前（[`agent/memory/processor.py:26`](../../../../agent/memory/processor.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `text` | `str` | 必填 | 模型原始输出，可能被 ``` 或 ```json 包裹 |

隐式输入：无。

功能与内部调用：

1. `text = (text or "").strip()`；
2. 若以 ` ``` ` 开头：取 `splitlines()[1:]`，若最后一行 `strip()` 后恰为 ` ``` ` 则去掉，最后用 `"\n".join(...).strip()` 返回；
3. 否则原样返回（已 `strip`）。

输出：去围栏后的内容（首尾空白被清理）。副作用：无。

异常与边界：只处理“以围栏开头”的情况；围栏后仍有正文、语言标记（```json）会被首行一并丢弃；`text` 为 `None` 时返回 `""`；不会校验内容是否为合法 JSON。

后续去向：返回调用方后进入 `json.loads` / Pydantic 校验。

## 分支与异常链

| 条件 | 行为 | 终点 |
| --- | --- | --- |
| `content` 为列表且含 dict 文本块 | 提取 `text` 字段拼接 | R1 返回 |
| `content` 为 `None`/空串 | 返回 `""` | R1 返回 |
| 文本无 `<timestamp>` | 原样返回 | R2 返回 |
| 多个时间戳 | 全部删除 | R2 返回 |
| 文本不以 ``` 开头 | 仅去首尾空白 | R3 返回 |
| 围栏无结尾 ``` | 保留围栏后所有行 | R3 返回 |
| 围栏语言标记 `json` | 与首行一起丢弃 | R3 返回 |

## 输入输出示例

与 `tests/test_module_layout.py:47-57` 的断言一致：

```text
content_text(["甲", {"type": "text", "text": "乙"}, {"type": "image_url", "image_url": "unused"}])
  → "甲乙"
content_text(None) → ""

strip_timestamps("<timestamp>今天</timestamp>\n正文") → "正文"
strip_timestamps("<reply>正文</reply>") → "<reply>正文</reply>"      # 非 timestamp 标签保留

strip_code_fence('```json\n{"x": 1}\n```')   → '{"x": 1}'
strip_code_fence('```\r\n{"x": 1}\r\n```')   → '{"x": 1}'
strip_code_fence('```json\n{"x": 1}')        → '{"x": 1}'            # 无结尾围栏也去掉首行
strip_code_fence(' {"x": 1} ')               → '{"x": 1}'            # 无围栏仅去空白
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)（agent/utils 总览）
- 相关模块：[`../chunking/README.md`](../chunking/README.md)（R3 消费）、[`../../node/draft/README.md`](../../node/draft/README.md)、[`../../node/check/README.md`](../../node/check/README.md)、[`../../node/participant_state/README.md`](../../node/participant_state/README.md)、[`../../node/tts/README.md`](../../node/tts/README.md)
- 已有测试覆盖（本次未执行）：`tests/test_module_layout.py:47-57`
- 验证情况：本页为静态阅读源码所得；调用方清单按当前 import 与调用点逐一核对，未在本次文档编写中实际执行测试。
