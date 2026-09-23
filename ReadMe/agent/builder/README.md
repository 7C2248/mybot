# 图构建（agent/builder.py）

## 职责与入口

`agent/builder.py` 是 Agent 图的构建模块（不是图节点）：把节点工厂、工具、路由函数注册进 `StateGraph(AgentState)`，并编译为可执行图。

- 源码：[agent/builder.py](../../../agent/builder.py)
- 主要调用方：`server/services/agent.py` 的 `AgentAdapter.graph/execute`（[server/services/agent.py](../../../server/services/agent.py)）、`server/services/legacy.py` 的导入校验、测试夹具。
- 对外函数：`build_rp_agent`（主构建入口）与 `build_cli_agent`（兼容包装）。

## 调用链总览

| 阶段 | 步骤 | 关系 |
| --- | --- | --- |
| 构建 | B1 `build_rp_agent` | 工厂函数，读取依赖并注册节点 |
| 构建 | B1.1 记忆存储与队列初始化 | 条件创建 `AsyncPostgresCharacterMemoryStore` / `MemoryJobRepository` |
| 构建 | B1.2 工具与角色档案 | `build_default_tools` / `load_character_profile` |
| 构建 | B1.3 `add_node` | 注册节点；服务模式下包装 `observed` 回调 |
| 构建 | B1.4 注册边与条件边 | 固定边、`draft_judge`/`check_judge`/`event_judge` |
| 运行 | R1 图调度 | LangGraph 按边执行节点，本模块不参与运行时 |

## 构建链

### B1. `build_rp_agent`

- 定位与签名：`build_rp_agent(character_name: str, *, pool=None, checkpointer=None, memory_jobs=None, character_profile=None, before_node=None, on_commit=None, recovery_only=False, memory_retrieval_enabled=True, memory_storage_enabled=True, memory_source=None)`，异步函数，[agent/builder.py:25](../../../agent/builder.py#L25)。
- 调用方与条件：`AgentAdapter.graph` 为每次运行调用；`LegacyService` 导入校验使用 `recovery_only=True`。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `character_name` | `str` | 必填 | 角色目录名，用于记忆表、档案、Prompt 和 TTS 语音档案 |
| `pool` | 连接池或 `None` | 默认 `None` | Postgres 连接池；为 `None` 时跳过记忆存储与队列初始化 |
| `checkpointer` | LangGraph checkpointer 或 `None` | 默认 `None` | 传给 `workflow.compile`；服务模式为持有执行器连接的 `AsyncPostgresSaver` |
| `memory_jobs` | `MemoryJobRepository` 或 `None` | 默认 `None` | 记忆任务仓储；为 `None` 且需要记忆时用 `pool` 创建 |
| `character_profile` | `str` 或 `None` | 默认 `None` | 角色档案快照；服务模式传入当次运行读取的文本 |
| `before_node` | `async (name, state)` 或 `None` | 默认 `None` | 每个节点执行前的观察回调（服务端发布 phase、检查停止/连接） |
| `on_commit` | `async (state, output)` 或 `None` | 默认 `None` | `commit_reply` 执行后的提交回调（服务端落库正式回复） |
| `recovery_only` | `bool` | 默认 `False` | 只构建空图用于 checkpoint 修复，不创建依赖、不执行逻辑 |
| `memory_retrieval_enabled` | `bool` | 默认 `True` | 控制 `memory_query` 工具是否加入默认工具集 |
| `memory_storage_enabled` | `bool` | 默认 `True` | 控制是否初始化记忆存储与队列 |
| `memory_source` | `async (state)` 或 `None` | 默认 `None` | 受管会话的正式消息来源；传给 `prepare_memory` 节点工厂 |

隐式输入：`config/models.yaml`（节点模型）、`Character/<角色>/` 档案、`config.config` 中的开关；`recovery_only=True` 时均不读取。

功能与内部调用：

1. **B1.1 记忆存储与队列**：当 `pool is not None and not recovery_only and (memory_retrieval_enabled or memory_storage_enabled)` 时，延迟导入并调用 `AsyncPostgresCharacterMemoryStore.create(pool, character_name)`；若 `memory_jobs is None` 再调用 `MemoryJobRepository.create(pool)`。创建失败会向上抛出，由服务端决定运行失败或降级。
2. **B1.2 工具与档案**：`build_default_tools(memory_store=memory_store if memory_retrieval_enabled else None)`；`recovery_only=True` 时工具为空列表。若未传入 `character_profile` 且非恢复模式，调用 `load_character_profile(character_name)`。因此只存储不检索时仍会创建 store，但不会暴露 `memory_query` 工具；工具清单见 [../tools/README.md](../tools/README.md)。
3. **B1.3 注册节点**：创建 `StateGraph(AgentState)` 后，通过内部 `add_node(name, factory)` 注册全部节点：

| 图节点名 | 工厂/执行对象 | 说明 |
| --- | --- | --- |
| `begin_turn` | `begin_turn` | 轮初重置与重试去重 |
| `apply_memory_results` | `create_apply_memory_results_node(memory_jobs, character_name)` | 应用后台记忆结果 |
| `limit_context` | `limit_context` | 模型上下文裁剪 |
| `world_state_update` | `update_world_state` | 世界状态更新 |
| `participant_state_in` | `create_participant_state_node(trigger="user")` | 用户输入后的双方状态更新 |
| `tools` | `ToolNode(tools)` | 工具执行（LangGraph 预置） |
| `draft` | `create_draft_node(character_name, character_profile, tools)` | 主回复候选生成 |
| `check` | `create_check_node()` | 独立检查 |
| `commit_reply` | `commit_reply` | 正式提交 |
| `reply_failed` | `reply_failed` | 失败收尾 |
| `participant_state_out` | `create_participant_state_node(trigger="reply")` | 正式回复后的双方状态更新 |
| `update_iter` | `increment_iteration` | 成功轮数计数 |
| `tts` | `create_tts_node(character_name)` | 语音合成/播放 |
| `prepare_memory` | `create_prepare_memory_node(character_name, memory_source)` | 生成记忆快照 |
| `enqueue_memory` | `create_enqueue_memory_node(memory_jobs)` | 投递记忆任务 |

4. **B1.4 注册边**：`set_entry_point("begin_turn")` 后按“整体流程”注册固定边与条件边，最后 `workflow.compile(checkpointer=checkpointer)` 返回编译结果。

输出：编译后的 LangGraph 图对象（`CompiledStateGraph`）。运行阶段由 `AgentAdapter.execute` 以 `astream(..., stream_mode="updates", durability="sync")` 驱动。

副作用、异常与去向：构建期会创建数据库存储对象、加载本地模型配置（`get_node_model` 延迟到节点首次执行）并读取角色档案；异常直接向上抛出。`recovery_only=True` 时所有节点被替换为 `lambda state: {}`，仅用于读取/修复 checkpoint。

### B1.3.1 `add_node` 与 `observed` 包装

- 定位：`build_rp_agent.<locals>.add_node` / `observed`，[agent/builder.py:63](../../../agent/builder.py#L63)。
- 行为：
  - `recovery_only=True`：注册空操作节点。
  - 无 `before_node` 且无 `on_commit`：直接注册工厂返回的节点（CLI 旧用法/测试）。
  - 有观察回调：把节点包装为 `RunnableLambda`，注册 `observed(state, config)`；它先 `await before_node(name, state)`，再 `await runnable.ainvoke(state, config)`；仅当 `name == "commit_reply"` 且 `on_commit` 存在时调用 `on_commit(state, result)`，然后返回结果。
- 关键点：`commit_reply` 的落库回调发生在图把结果合并进 checkpoint 之前，保证“正式回复先入库，再允许推进/裁剪”。
- 输出：节点返回值原样作为状态增量交给 LangGraph 合并；异常向上抛出，由服务执行器转为运行失败并触发修复。

### B2. `build_cli_agent`

- 定位与签名：`build_cli_agent(character_name, *, pool=None, checkpointer=None, memory_jobs=None)`，异步函数，[agent/builder.py:131](../../../agent/builder.py#L131)。
- 行为：直接委托 `build_rp_agent`，不额外配置观察回调。
- 现状：CLI 已改为 HTTP 客户端（`main.py` → [cli/client.py](../../../cli/client.py)），当前仓库内没有调用方，属于保留的兼容入口。

## 图调度关系

运行阶段的关系是“框架调度”，不是直接函数调用：

| 起点 | 条件（路由函数） | 目标节点 | 条件来源 |
| --- | --- | --- | --- |
| `draft` | `draft_judge` 返回 `"tools"` | `tools` | `draft_status == "calling_tools"` |
| `draft` | `draft_judge` 返回 `"check"` | `check` | `draft_status == "ready"` |
| `draft` | `draft_judge` 返回 `"reply_failed"` | `reply_failed` | 其他状态（含 `"failed"`） |
| `tools` | 固定边 | `draft` | 工具消息写入 `messages` 后回到主模型 |
| `check` | `check_judge` 返回 `"commit_reply"` | `commit_reply` | `check_status == "passed"` |
| `check` | `check_judge` 返回 `"draft"` | `draft` | `check_status == "failed"` 且 `check_rounds < 5` |
| `check` | `check_judge` 返回 `"reply_failed"` | `reply_failed` | 其他情况（含 `"unavailable"` 与轮数耗尽） |
| `tts` | `event_judge` 返回 `True` | `prepare_memory` | 记忆存储开启、无挂起任务且满足轮数/token 条件 |
| `tts` | `event_judge` 返回 `False` | `END` | 其他情况 |
| `reply_failed` / `enqueue_memory` | 固定边 | `END` | 本轮结束 |

路由函数的实际条件与输出字段见各节点叶子文档；`draft_judge`/`check_judge` 见 [../node/draft/README.md](../node/draft/README.md)、[../node/check/README.md](../node/check/README.md)，`event_judge` 见 [../node/event/README.md](../node/event/README.md)。

## 分支与异常链

- **恢复模式（B1 的 `recovery_only=True`）**：不加载工具/档案/记忆，所有节点为空操作；`AgentAdapter.repair` 用它读取 checkpoint 状态后写回修复结果，不会重放挂起任务。
- **记忆关闭**：`memory_store=None` 时 `memory_query` 不注册，`draft` 在系统提示词中声明“未提供记忆检索工具”；`memory_storage_enabled=False` 时 `prepare_memory`/`enqueue_memory`/`apply_memory_results` 均直接返回 `{}`，`event_judge` 返回 `False`。
- **构建期异常**：模型配置缺失、角色档案缺失、数据库不可用都会在 B1 抛出；服务端将运行标记为失败，不做图内降级。

## 关联文档与验证依据

- 上级：[Agent 总览](../README.md)
- 节点：[../node/README.md](../node/README.md) · 协议：[../classes/state/README.md](../classes/state/README.md)
- 服务集成：[server/services/agent.py](../../../server/services/agent.py)
- 依据：`agent/builder.py` 的实际注册关系；`tests/test_rp_pipeline.py`、`tests/test_module_layout.py` 对节点/路由有离线覆盖；本次未执行测试。
