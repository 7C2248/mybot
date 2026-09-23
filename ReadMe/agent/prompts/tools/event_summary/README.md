# event_summary 提示词

## 职责与入口

- 所属类别：提示词模块（非图节点）。源码：[agent/prompts/tools/event_summary.py](../../../../../agent/prompts/tools/event_summary.py)。
- 注册与导出：`tools/__init__.py` 导入并列入 `__all__`（[agent/prompts/tools/__init__.py:6](../../../../../agent/prompts/tools/__init__.py)）；`agent/prompts/__init__.py` 再次导出，并提供 `get_prompt("event_summary")` 分发（[agent/prompts/__init__.py:36](../../../../../agent/prompts/__init__.py)）。
- 获取函数：`get_event_summary_prompt(language: str = "zh") -> str`（[event_summary.py:9](../../../../../agent/prompts/tools/event_summary.py)）。
- 消费方：`agent/memory/processor.py` 的 `process_memory_snapshot`（[agent/memory/processor.py:197](../../../../../agent/memory/processor.py)）。
- 触发时机：图节点 `enqueue_memory` 入队后，由独立后台 Worker 领取任务执行（[agent/memory/worker.py:52](../../../../../agent/memory/worker.py)），不在本轮图调度内同步完成。

## 调用链总览

```text
（图内）tts --event_judge 为 True--> prepare_memory → enqueue_memory → END（入队返回）
（后台）MemoryWorker._process → process_memory_snapshot(payload, memory_store)
    → get_prompt("event_summary")                    （本提示词，SystemMessage）
    → 追加 scope 说明、world_state、检索到的 memories、格式化 history_messages
    → get_node_model("memory_summary").bind_tools(tools).ainvoke(...)
    → 校验并转换工具调用为 MemoryPlan
    → MemoryJobRepository.finish 统一提交（DB 写入不在提示词链内）
```

## 获取链

### B1. `get_event_summary_prompt`

- 定位与签名：`agent.prompts.tools.event_summary.get_event_summary_prompt(language: str = "zh") -> str`，同步函数，[源码 event_summary.py:9](../../../../../agent/prompts/tools/event_summary.py)。
- 调用方与条件：`process_memory_snapshot` 每次处理任务时通过 `get_prompt("event_summary")` 获取一次（[agent/memory/processor.py:198](../../../../../agent/memory/processor.py)）；未传 `language`，取缺省 `"zh"`。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `language` | `str` | 默认 `"zh"` | 语言标识；`normalize_language` 后为 `"en"` 时返回英文分支 |

- 隐式输入：`normalize_language`（[agent/utils/language.py:4](../../../../../agent/utils/language.py)）。
- 功能与内部调用：归一语言；`"en"` 时调用 `_get_event_summary_prompt_en()`（[event_summary.py:15](../../../../../agent/prompts/tools/event_summary.py)，返回空字符串），否则调用 `_get_event_summary_prompt_zh()`（[event_summary.py:18](../../../../../agent/prompts/tools/event_summary.py)）。
- 输出：完整记忆总结系统提示词字符串。
- 副作用与异常：无；不读文件、不调模型、不抛异常。

生成内容概要（不复制原文）：

| 部分 | 内容 |
| --- | --- |
| 角色定位 | 角色扮演智能体的长期记忆管理器；依据最新对话对记忆库执行 add/update/delete，保持准确、非冗余，全部用角色第一人称 |
| 输入结构 | 声明三类输入：`<world_state>`（日期/星期/时间段/位置/天气）、`<memories>`（`id,importance: memory text`，按重要性与更新时间降序）、`<history_messages>`（带序号与说话者标识，工具消息已被外部跳过） |
| 重要性评分 | 0~100 整数，取情绪强度、关系里程碑、独特性、长期影响四维中的最高分而非平均；附正反示例 |
| 核心流程 | 提取有价值信息并评分 → 与已有记忆比较冲突/合并/评分合理性 → 风格清洗（感官、比喻、具身情绪、对白复原、文学句式、心理独白、主观评价、相对时间逐项改写）→ 选择工具（insert/update/delete/clean_history/ignore）→ 单次回复内完成所有调用 |
| 记忆范围 | 应记住角色自身、对话对象、剧情与承诺；不记寒暄、琐碎动作、重复内容、语气修辞、氛围与瞬时心理过程 |
| 操作细则 | add/update/delete 的适用条件、冲突时以最新对话为准、只能使用输入已有 ID、event_date 与 keywords 由系统自动提取 |
| 寄存器规范 | 日志体、绝对时间锚定、保留第一人称“我”；禁止感官描写、比喻、情绪宣泄、对白复原、文学句式、心理独白、主观评价；情绪与关系以标签集合表达；附记忆文本推荐格式与 6 组正反示例；更新合并时须清洗旧记忆的污染表述 |

## 运行链

### R1. `process_memory_snapshot`（提示词消费）

- 定位与签名：`agent.memory.processor.process_memory_snapshot(payload: dict, memory_store) -> MemoryPlan`，异步；[agent/memory/processor.py:163](../../../../../agent/memory/processor.py)。
- 调用方与条件：后台 `MemoryWorker._process` 在持有会话写锁期间调用（[agent/memory/worker.py:52](../../../../../agent/memory/worker.py)）；任务在 `enqueue_memory` 时已入队，本轮图已结束。
- 前置校验：`payload["version"] == 1`、`memory_storage_enabled` 为真、`new_message_start` 在消息范围内，否则抛 `ValueError`。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `payload["messages"]` | `list[dict]` | 入队时的消息快照 | 经 `messages_from_dict` 还原 | 作为 `<history_messages>` |
| `payload["new_message_start"]` | `int` | 入队时计算 | 必须落在范围内 | 标记本次新增消息起点，写入 scope 说明 |
| `payload["world_state"]` | `dict \| None` | 入队快照 | 经 `prepare_world_state` 转文本 | 作为 `<world_state>` 与 scope 上下文 |
| `payload["memory_retrieval_enabled"]` | `bool` | 入队配置 | 默认 `True` | 决定检索与可用工具集 |
| `memory_store` | `AsyncPostgresCharacterMemoryStore` | Worker 构造 | 必填 | 检索与 `prepare_memory` 计算 |

- 隐式输入：提示词 B1 结果；模型 `get_node_model("memory_summary")`；工具定义 `_MEMORY_TOOLS`（[agent/memory/processor.py:76](../../../../../agent/memory/processor.py)）；`format_history`、`history_removals` 等工具函数。
- 功能与内部调用：
  1. 若启用检索，先按 `memory_query` 链路（见同级文档）生成查询并执行混合检索，得到 `related` 与 `known_ids`；
  2. 组装 `memories` 文本（`id,importance: memory` 行），并生成 scope 说明（新增消息起点、旧消息仅作背景、clean_history 编号规则）；
  3. 选择工具集：启用检索时为 insert/update/delete/clean_history 四件；关闭检索时仅 insert/clean_history，并追加“只提取新增记忆”的说明；
  4. `get_node_model("memory_summary").bind_tools(tools).ainvoke([SystemMessage(event_summary), SystemMessage(scope), HumanMessage(state_text), HumanMessage(memories), HumanMessage(history)])`；
  5. 校验工具调用：`invalid_tool_calls` 直接抛错；`clean_history.index` 必须为正整数；update/delete 的 `memory_id` 必须来自本次检索；同一计划不得重复修改同一 ID；文本非空；importance 必须是 0~100 整数；
  6. 对每个写入调用 `memory_store.prepare_memory`（其中触发分块提示词链），组装 `MemoryPlan`。
- 输出与状态字段：

| 输出 | 类型 | 产生条件 | 含义与消费方 |
| --- | --- | --- | --- |
| `plan.remove_ids` | `list` | `clean_history` 调用时 | 由 `history_removals` 计算待删消息 ID；`jobs.finish` 在事务中提交 |
| `plan.operations` | `list[MemoryOperation]` | insert/update/delete 调用时 | 含名称、memory_id 与 `PreparedMemory`；由 `jobs.finish` 写入数据库 |
| 抛出异常 | - | 校验失败 | Worker 捕获后调用 `jobs.fail` 记录失败，任务保留在队列 |

- 副作用：模型请求、检索请求、嵌入与分块计算；数据库写入由 Worker 的 `finish` 统一提交，处理器本身不写库。
- 异常与边界：参数无效、工具越权（关闭检索时出现 update/delete）等均抛 `ValueError`；Worker 按 `max_attempts=5` 记录失败，不删除任务。
- 后续去向：`MemoryJobRepository.finish` 提交结果；任务完成后图侧由 `apply_memory_results` 在后续轮次应用记忆变更。

## 分支与异常链

- **关闭检索**：工具集缩减为 insert/clean_history，scope 追加限制说明；update/delete 出现即报错。
- **无相关记忆**：`<memories>` 仍会输出空块，模型按无旧记忆处理，只做新增与裁剪。
- **模型输出自由文本而无工具调用**：`response.tool_calls` 为空时计划为空，不产生任何变更，任务正常完成。
- **工具参数非法**：先集中校验全部调用再计算，避免参数错误时进行不必要的切块与编码（[agent/memory/processor.py:205](../../../../../agent/memory/processor.py)）。
- **EN 语言**：提示词为空字符串；当前调用方未传 `language`，不会进入该分支。

## 输入输出示例

适用 R1（示意，非真实内容）：

```text
SystemMessage 1：event_summary 提示词（中文）
SystemMessage 2：本次新增消息从 history_messages 的 index:6 开始……
HumanMessage 1：<world_state>{"date": "2026-09-23", ...}</world_state>
HumanMessage 2：<memories>\n3,80: 2026-08-01 我和[用户]约定周末见面。\n</memories>
HumanMessage 3：<history_messages>\nindex:6 User: ……\nindex:7 Character: ……\n</history_messages>
```

期望的模型行为是工具调用（参数定义由处理器内工具描述提供），例如：

```json
{"name": "insert_memory", "args": {"memory": "2026年9月23日 家中。[用户]告诉我周末要加班，原定见面推迟。情绪：失落。", "importance": 55}}
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md)
- 相关提示词：[../memory_query/README.md](../memory_query/README.md)、[../event_judge/README.md](../event_judge/README.md)
- 消费源码：[agent/memory/processor.py](../../../../../agent/memory/processor.py)、[agent/memory/worker.py](../../../../../agent/memory/worker.py)
- 记忆服务：[../../../memory/README.md](../../../memory/README.md)
- 验证依据：静态阅读源码；本次未执行测试。
