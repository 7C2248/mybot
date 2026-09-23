# messages — 对话消息的共享序列化

## 职责与入口

- 所属类别：`agent/utils/` 支撑模块，**不是图节点**，无图注册名、无路由。
- 源码：[`agent/utils/messages.py`](../../../../agent/utils/messages.py)（模块 docstring：Agent 对话消息的共享序列化）。
- 职责：把 LangChain 消息对象列表渲染为 `<history_messages>` 文本块，供后台记忆处理器拼进模型提示词；可选择是否保留工具调用与工具结果的标记行。
- 公开入口：`format_history`，单函数模块，无内部辅助函数。
- 调用方式：同步函数，由 `agent/memory/processor.py` 在后台记忆计算阶段调用。

## 调用链总览

```text
R1 format_history(messages, include_tools=False) -> str

调用方（均在 agent/memory/processor.py，后台 Worker 上下文）
  记忆检索语句生成：format_history(query_context[-120:])                ← include_tools=False
  记忆总结模型：format_history(messages, include_tools=True)            ← 需要工具调用标记
```

## 构建链

无工厂、无缓存、无依赖注入；仅依赖 `langchain_core.messages` 的三个消息类型。

## 运行链

### R1. `format_history`

- 定位与签名：`format_history(messages: list, include_tools: bool = False) -> str`，[`agent/utils/messages.py:6`](../../../../agent/utils/messages.py)，同步函数。
- 调用方与条件：
  - `process_memory_snapshot` 生成检索语句时：`format_history(query_context[-120:])`，只截取新增范围前 6 条起的最近消息，不显示工具标记（[`agent/memory/processor.py:180`](../../../../agent/memory/processor.py)）；
  - `process_memory_snapshot` 调用总结模型时：`format_history(messages, include_tools=True)`，传入完整快照消息（[`agent/memory/processor.py:201`](../../../../agent/memory/processor.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `messages` | `list`（LangChain 消息对象） | 必填 | 消息窗口；来自 `messages_from_dict(payload["messages"])` 或切片 |
| `include_tools` | `bool` | 默认 `False` | 是否输出工具调用与工具结果行 |

隐式输入：无。

功能与内部调用：

1. 以 `"<history_messages>\n"` 开头，以 `"</history_messages>\n"` 结尾；
2. 按下标 `index` 逐条处理：
   - `AIMessage` 且 `len(message.tool_calls) == 0` → `index:{i} Character:{message.content}`；
   - `AIMessage` 且有工具调用且 `include_tools=True` → `index:{i} Toolcalls`（不输出调用参数与正文）；
   - `HumanMessage` → `index:{i} User:{message.content}`；
   - `ToolMessage` 且 `include_tools=True` → `index:{i} ToolUsage:{message.name}`；
   - 其他情况（`include_tools=False` 的带工具调用 AI 消息、未开启的工具消息、其他消息类型）跳过，不占输出行但 `index` 仍按原始位置计数。

输出：`str`，每行以 `\n` 结尾，下标从 0 开始且保留原始消息位置（跳过的消息会造成下标不连续）。

副作用：无。

异常与边界：`message.content` 为列表（多模态）时会被 f-string 转成 Python 字面量文本，不做清洗；`message.name` 为 `None` 时输出 `ToolUsage:None`。本函数不剥离时间戳，`HumanMessage` / `AIMessage` 的 `<timestamp>` 会原样进入文本（后台处理器本身不额外清洗）。

后续去向：返回字符串作为 `SystemMessage` / `HumanMessage` 内容的一部分送入 `memory_query` 或 `memory_summary` 节点模型。

## 分支与异常链

| 条件 | 输出行 | 备注 |
| --- | --- | --- |
| AI 消息无工具调用 | `index:{i} Character:{content}` | 两种模式都输出 |
| AI 消息有工具调用，`include_tools=False` | 无 | 该消息完全不出现 |
| AI 消息有工具调用，`include_tools=True` | `index:{i} Toolcalls` | 不展示调用名与参数 |
| 用户消息 | `index:{i} User:{content}` | 两种模式都输出 |
| 工具消息，`include_tools=True` | `index:{i} ToolUsage:{name}` | 不展示工具返回内容 |
| 工具消息，`include_tools=False` | 无 | 检索语句生成阶段不暴露工具结果 |
| 其他消息类型 | 无 | 仅 `AIMessage` / `HumanMessage` / `ToolMessage` 有分支 |

## 输入输出示例

与 `tests/test_module_layout.py:82-93` 的断言一致（示意消息）：

```text
messages = [HumanMessage("你好"),
            AIMessage("", tool_calls=[{"id": "call", "name": "memory_query", "args": {}}]),
            ToolMessage("事实", tool_call_id="call", name="memory_query"),
            AIMessage("回复")]

format_history(messages) →
<history_messages>
index:0 User:你好
index:3 Character:回复
</history_messages>

format_history(messages, include_tools=True) →
<history_messages>
index:0 User:你好
index:1 Toolcalls
index:2 ToolUsage:memory_query
index:3 Character:回复
</history_messages>
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)（agent/utils 总览）
- 调用方文档：[`../../memory/processor/README.md`](../../memory/processor/README.md)
- 调用方源码：[`agent/memory/processor.py:11`](../../../../agent/memory/processor.py)
- 已有测试覆盖（本次未执行）：`tests/test_module_layout.py:82-93`（工具标记与原始下标）
- 验证情况：本页为静态阅读源码所得；输出格式按函数体逐行核对，未在本次文档编写中实际执行测试。
