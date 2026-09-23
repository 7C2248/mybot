# agent/classes —— Agent 状态、协议与共享类型

`agent/classes/` 是 Agent 的数据契约层：定义 LangGraph 全图共享状态 `AgentState`、各图节点的结构化输出协议、记忆检索工具的输入输出协议、后台记忆计算产物以及 LangChain 文档重排组件。本目录不注册图节点、不访问数据库、不发起模型调用；它只提供类型与校验规则，由 `agent/builder.py`、`agent/node/*`、`agent/tools/*`、`agent/memory/*` 与 `server/services/*` 读写或实例化。`__init__.py` 仅含模块说明，无运行时代码。

## 子目录与节点

| 名称 | 类型 | 主要功能 | 协作对象 | 源码 | 文档 |
| --- | --- | --- | --- | --- | --- |
| `AgentState` | 图状态协议 | 定义全图共享字段、`messages` 的 `add_messages` reducer 及记忆交接字段 | `agent/builder.py` 以 `StateGraph(AgentState)` 注册；所有图节点、`server/services/agent.py`、`server/services/checkpoint_sync.py` | [`state.py`](../../../agent/classes/state.py) | [`state/README.md`](state/README.md) |
| `CheckIssue` / `CheckResult` | 节点结构化输出协议 | 约束 check 节点的检查结论：结论与问题列表必须一致；模型路径的类型受限且须给出可定位片段与修改建议 | `agent/node/check.py` 的模型输出与本地规则；`agent/node/draft.py` 读取反馈 | [`check.py`](../../../agent/classes/check.py) | [`check/README.md`](check/README.md) |
| `UserStateUpdate` / `CharacterStateUpdate` / `ParticipantStateUpdate` | 节点结构化输出协议 | 严格模式校验一次模型调用返回的角色与用户状态增量，支持显式 `null` 清除字段 | `agent/node/participant_state.py`（`participant_state_in` / `participant_state_out`） | [`participant_state.py`](../../../agent/classes/participant_state.py) | [`participant_state/README.md`](participant_state/README.md) |
| `MemorySearchQuery` / `MemoryQueryInput` / `MemoryHit` / `MemorySearchResult` | 工具输入输出协议 | 约束 `memory_query` 工具的检索参数（关键词序列与日期区间）及结构化结果（`ok` / `empty` / `error`） | `agent/tools/memory_query.py`；`server/services/agent.py` 解析工具结果发布事件 | [`memory.py`](../../../agent/classes/memory.py) | [`memory/README.md`](memory/README.md) |
| `PreparedMemory` / `MemoryOperation` / `MemoryPlan` | 后台数据产物（dataclass） | 在“模型计算”与“数据库提交”之间传递不可变的记忆写操作与裁剪计划，不含连接和可变图状态 | `agent/memory/processor.py` 生产；`agent/memory/jobs.py`、`agent/memory/store.py` 提交；`agent/memory/worker.py` 串联 | [`memory_job.py`](../../../agent/classes/memory_job.py) | [`memory_job/README.md`](memory_job/README.md) |
| `CrossEncoderReranker` | 检索组件（LangChain `BaseDocumentCompressor`） | 用 HuggingFace 交叉编码器对候选父记忆打分并按分数取前 `top_k` 条 | `agent/utils/models.py` 构造；`agent/memory/store.py` 的 `search_hybrid` / `_rerank` 调用 | [`reranker.py`](../../../agent/classes/reranker.py) | [`reranker/README.md`](reranker/README.md) |

## 整体流程

本目录不承载执行流程，按“定义 → 使用方 → 所支撑的功能”表达协作关系：

```text
定义（本目录）                              使用方                                    所支撑的功能
─────────────────────────────────────────────────────────────────────────────────────────────────────
AgentState (state.py)               → builder.StateGraph + 全部图节点           → 对话上下文、迭代进度、记忆交接
CheckIssue / CheckResult (check)    → agent/node/check.py                      → 候选回复的规则检查与模型检查、修订反馈
ParticipantStateUpdate              → agent/node/participant_state.py          → 角色/用户状态的原子更新
MemoryQueryInput / MemoryHit /      → agent/tools/memory_query.py              → 主模型自主检索长期记忆；
MemorySearchResult (memory)         → server/services/agent.py                 → 工具结果的 UI 事件投影
PreparedMemory / MemoryOperation /  → agent/memory/processor.py                → 后台 Worker 计算与一次事务提交；
MemoryPlan (memory_job)             → agent/memory/store.py / jobs.py          → 结果回执后的轮初裁剪
CrossEncoderReranker (reranker)     → agent/utils/models.py                    → search_hybrid 的粗排候选精排
                                    → agent/memory/store.py
```

边界：本目录只定义数据形状与校验，不决定何时读写。何时产生 `CheckResult`、何时消费 `MemoryPlan` 由对应叶子文档（`../node/check/README.md`、`../memory/README.md` 等）说明。

## 主要数据与依赖

- `AgentState` 是唯一的图级状态，由 checkpointer 按 channel 持久化；其中 `messages` 走 `add_messages`（追加、按 ID 替换、`RemoveMessage` 删除），其余字段为覆盖写。记忆相关字段在轮初不重置，与消息裁剪共同落盘。
- `CheckResult`、`ParticipantStateUpdate`、`MemorySearchResult` 是进程内的数据对象：分别来自 check 模型、participant_state 模型和记忆检索工具，序列化后进入 `AgentState`、`ToolMessage` 或服务端事件。
- `MemoryPlan` / `PreparedMemory` 跨“图进程 → Worker 进程”传递：`processor` 只计算，`jobs.finish` 在同一事务中调用 `store.apply_operations` 写库并保存结果回执。
- `CrossEncoderReranker` 依赖 `langchain_core` 的 `BaseDocumentCompressor`、`langchain_community` 的 `HuggingFaceCrossEncoder` 与本地 BGE 模型权重，由 `agent/utils/models.py` 延迟加载并缓存。
- 本目录不直接依赖数据库、Prompt 或模型 API；这些依赖全部由使用方注入。

## 阅读导航

- 上级：[`../README.md`](../README.md)（Agent 总览）
- 协作模块：[`../builder/README.md`](../builder/README.md)、[`../node/README.md`](../node/README.md)、[`../memory/README.md`](../memory/README.md)、[`../tools/README.md`](../tools/README.md)、[`../utils/README.md`](../utils/README.md)
- 相关专题：[`../memory/README.md`](../memory/README.md)（记忆服务与后台交接）、[`../../../data/docs/LangGraph_CheckPoint_Postgres.md`](../../../data/docs/LangGraph_CheckPoint_Postgres.md)（checkpoint 持久化参考，存放于本地 `data/docs/`）
