# context — 文本与消息 Token 估算

## 职责与入口

- 所属类别：`agent/utils/` 支撑模块，**不是图节点**，无图注册名、无路由。
- 源码：[`agent/utils/context.py`](../../../../agent/utils/context.py)（模块 docstring：Token estimates shared by context limiting and memory routing）。
- 职责：在不调用分词器的前提下，按 UTF-8 字节数估算文本和消息的 token 数量，供上下文裁剪和记忆路由使用。
- 公开入口：`estimate_tokens_from_bytes`（纯文本）与 `estimate_message_tokens`（消息，含工具调用参数）。
- 调用方式：同步函数，由 `limit_context` 与 `event_judge` 在每轮运行时直接调用。

## 调用链总览

```text
R1 estimate_tokens_from_bytes(text)
  └─ 被 R2 调用，也被 agent/node/event.py 以别名导入
       （event.py 当前只在测试中直接调用该别名）

R2 estimate_message_tokens(message)
  ├─ R2.1 取 message.content（list 时 json.dumps）
  ├─ R2.2 拼接 message.tool_calls 的 args JSON
  └─ R1 estimate_tokens_from_bytes(text)

调用方
  agent/node/context.py limit_context        → R2（逐条消息求预算）
  agent/node/event.py _context_tokens        → R2（无 usage_metadata 时估算全部消息）
```

## 构建链

无工厂、无缓存、无依赖注入；两个函数都是无状态的纯计算。

## 运行链

### R1. `estimate_tokens_from_bytes`

- 定位与签名：`estimate_tokens_from_bytes(text: str) -> int`，[`agent/utils/context.py:6`](../../../../agent/utils/context.py)，同步函数。
- 调用方与条件：
  - R2 内部无条件调用；
  - `agent/node/event.py` 以 `_estimate_tokens_from_bytes` 别名导入（[`agent/node/event.py:8`](../../../../agent/node/event.py)），但该模块的运行逻辑只调用 `_estimate_message_tokens`；别名当前仅被 `tests/test_event_judge.py` 直接断言。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `text` | `str` | 必填 | 待估算文本；空串、`None` 等假值直接返回 0 |

隐式输入：无。

功能与内部调用：

1. `if not text: return 0`。
2. `data = text.encode('utf-8')`。
3. `ascii_bytes = sum(1 for byte in data if byte < 128)`。
4. 返回 `max(1, ascii_bytes // 4 + (len(data) - ascii_bytes) // 2)`。

即：ASCII 字节按 4 字节/token 折算，非 ASCII 字节按 2 字节/token 折算，两项向下取整后相加；只要输入非空，结果至少为 1。

输出：`int` token 估算值。

副作用：无。

异常与边界：非字符串输入（如 `None` 以外的对象）在 `.encode` 处抛 `AttributeError`；调用方均传入字符串或先做空值处理。估算不考虑标点、空格与实际分词差异，只用于阈值比较。

后续去向：返回 R2；或由测试直接断言。

### R2. `estimate_message_tokens`

- 定位与签名：`estimate_message_tokens(message) -> int`，[`agent/utils/context.py:14`](../../../../agent/utils/context.py)，同步函数。
- 调用方与条件：
  - `agent/node/context.py` 的 `limit_context`：对剩余消息逐条求和得到预算，并在按用户轮次推进裁剪游标时逐段扣减（[`agent/node/context.py:25`](../../../../agent/node/context.py)、[`agent/node/context.py:30`](../../../../agent/node/context.py)）；
  - `agent/node/event.py` 的 `_context_tokens`：从后往前找不到带 `usage_metadata` 的 AI 消息时，对全部消息逐条估算求和（[`agent/node/event.py:30`](../../../../agent/node/event.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `message` | LangChain 消息对象（`HumanMessage` / `AIMessage` / `ToolMessage` 等） | 必填 | 通常来自 `AgentState["messages"]`；读取 `content`、`tool_calls` 属性 |

隐式输入：无。

功能与内部调用：

1. `text = message.content or ''`。
2. 若 `text` 不是字符串（如多模态内容块列表），改为 `json.dumps(text, ensure_ascii=False)`。
3. 遍历 `getattr(message, 'tool_calls', None) or []`，把每个调用的 `call.get('args') or {}` 以 `json.dumps(..., ensure_ascii=False)` 追加到 `text`。
4. 调用 R1 返回估算值。

输出：`int` token 估算值；`content` 为空且无工具调用时返回 0（R1 收到空串）。

副作用：无。

异常与边界：`message` 为 `None` 时在第 1 步抛 `AttributeError`；`tool_calls` 元素缺少 `get` 方法（非 dict）时抛 `AttributeError`。当前调用方传入的都是 LangChain 消息对象。工具调用参数被计入估算，但工具调用的 `name` 与 `id` 不计入。

后续去向：`limit_context` 用它决定何时把裁剪游标推进到下一个用户轮次；`event_judge` 用它计算上下文压力是否达到 `MEMORY_TOKEN_THRESHOLD`。

## 分支与异常链

| 条件 | 行为 | 终点 |
| --- | --- | --- |
| `text` 为空/假值 | R1 返回 0 | 调用方预算不变 |
| 内容仅 ASCII | 按 `字节数 // 4` 折算 | R1 返回 |
| 内容含中文等非 ASCII | 非 ASCII 部分按 `字节数 // 2` 折算 | R1 返回 |
| 消息含 `tool_calls` | 工具参数 JSON 追加进估算文本 | R2 返回 |
| 消息内容为列表 | 整个列表序列化为 JSON 后估算 | R2 返回 |

## 输入输出示例

适用 R1（数值与 `tests/test_event_judge.py:89-92` 的断言一致）：

```text
estimate_tokens_from_bytes("")        → 0
estimate_tokens_from_bytes("a"*4000)  → 1000     # 4000 ASCII 字节 // 4
estimate_tokens_from_bytes("长"*400)  → 600      # 1200 非 ASCII 字节 // 2
```

适用 R2（示意，字段与实现一致）：

```text
message = AIMessage(content="", tool_calls=[{"name": "memory_query", "id": "c", "args": {"query": "约定"}}])
估算文本 = json.dumps({"query": "约定"}, ensure_ascii=False)   # 约 20 个非 ASCII 字节
输出 = 非空文本的字节估算值（>= 1）
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)（agent/utils 总览）
- 调用方源码：[`agent/node/context.py:3`](../../../../agent/node/context.py)、[`agent/node/event.py:6`](../../../../agent/node/event.py)
- 相关模块：[`../memory/README.md`](../memory/README.md)（同一轮次的记忆交接边界）
- 已有测试覆盖（本次未执行）：`tests/test_event_judge.py:89-101`（空串/ASCII/非 ASCII 数值、usage 优先与估算回退）、`tests/test_conversation_policy.py:66-79`（`limit_context` 在预算受限时按整轮裁剪）
- 验证情况：本页为静态阅读源码所得；公式与测试断言一致，未在本次文档编写中实际执行测试。
