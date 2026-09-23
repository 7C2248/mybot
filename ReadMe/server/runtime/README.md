# 对话与语音执行器：RunRuntime 与 AgentAdapter

## 职责与入口

本页覆盖 `server/services/runtime.py` 与 `server/services/agent.py`：对话/语音的独立执行线程、持久化运行队列的领取与恢复、LangGraph 图的适配（公开事件投影、正式回复先落库、检查点修复）。

| 文件 | 类型 | 职责 |
| --- | --- | --- |
| [server/services/runtime.py](../../../server/services/runtime.py) | 执行器 | `RunRuntime`：启动独立线程与事件循环、advisory lock、领取运行/语音、启动恢复、关闭 |
| [server/services/agent.py](../../../server/services/agent.py) | 图适配器 | `AgentAdapter`：构建图、`before_node`/`on_commit` 回调、`astream` 事件投影、`repair` |
| [server/services/memory_runtime.py](../../../server/services/memory_runtime.py) | 执行器 | `MemoryRuntime`：由 `RunRuntime.__init__` 创建、`start` 启动（详见 [memory 叶子](../memory/README.md)） |
| [agent/builder.py](../../../agent/builder.py) | 图构建 | `build_rp_agent`：本页只说明服务侧传入的依赖与回调，图内部见 [builder 叶子](../../agent/builder/README.md) |

上游：`app.py` lifespan（`runtime_factory(settings, catalog, models, gate)` → `runtime.start()`）；下游：`agent/builder.py` 编译的图、`RunRepository`/`SpeechRepository`（[conversations 叶子](../conversations/README.md)、[speech 叶子](../speech/README.md)）。

## 调用链总览

```text
构建阶段
B1 RunRuntime.__init__ ── 保存 settings/catalog/models/gate，创建 MemoryRuntime
B2 RunRuntime.start ── 启动 "mybot-runs" 线程并等待就绪；随后启动 MemoryRuntime
B3 RunRuntime._thread_main ── 新事件循环内运行 _serve()
B4 RunRuntime._serve（初始化部分）── advisory lock + checkpointer + 仓储 + adapter + speech + 启动恢复
B5 AgentAdapter.__init__ ── 注入 builder（默认 build_rp_agent）、pool、checkpointer、catalog、models、stopping
B6 AgentAdapter.graph ── 按需构建图（每次运行一次）

运行阶段（执行线程内循环）
R1 _serve 主循环 ── gate 非阻塞获取 → repo.claim() → _run_one；否则 speech_repo.claim() → speech.execute
R2 RunRuntime._run_one ── logging_context 包装后转 _execute_run
R3 RunRuntime._execute_run ── adapter.execute；失败时 repair + finish
R4 AgentAdapter.execute ── 快照配置/档案 → 构建图 → 组装 inputs → astream 投影 → 终态
  R4.1 before(name, state) 回调 ── 停止检查、连接存活检查、phase 事件
  R4.2 commit(state, output) 回调 ── 正式回复先 commit_reply 再允许图推进
  R4.3 memory_source(state) 回调 ── 从 UI 历史按策略版本取记忆源消息
R5 AgentAdapter.repair ── 恢复/重试前的检查点修复（不调用挂起节点）
R6 RunRuntime.close ── 停止标记、取消记忆、取消任务并等待线程
辅助：public_state / freeze_state / thaw_state / _input / _PHASES / _STATE_FIELDS
```

关系说明：`B6` 是构建关系（每次运行构建一次图）；`R4.1~R4.3` 是图通过 `builder.add_node` 的 `observed` 包装回调（图调度关系，非直接函数调用）；`R1` 的领取是数据库队列交接；`R5` 可在启动恢复、重试前和本轮失败时被调用。

## 构建链

### B1. `RunRuntime.__init__`

- 定位与签名：[server/services/runtime.py](../../../server/services/runtime.py) 的 `def __init__(self, settings, catalog, models, gate, *, adapter_factory=None, speech_factory=None, memory_worker_factory=None)`（同步）。
- 调用方与条件：`app.py` lifespan 中 `runtime_factory(settings, catalog, models, gate)`；`adapter_factory`/`speech_factory`/`memory_worker_factory` 供测试注入受控实现。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `settings` | `ServiceSettings` | 必填 | 端口无关；读取 `db_url`、`enable_runs`、`audio_root` |
| `catalog` | `CharacterCatalog` | 必填 | 角色档案/语音档案来源 |
| `models` | `ModelSettingsService` | 必填 | 提供 `snapshot()`、`memory_gate` 与生效版本 |
| `gate` | `threading.Lock` | 必填 | 对话/语音与模型配置应用互斥的共享门锁 |
| `adapter_factory` | `Callable \| None` | 默认 `None` | 为 `None` 时使用 `AgentAdapter` |
| `speech_factory` | `Callable \| None` | 默认 `None` | 为 `None` 时使用 `SpeechService` |
| `memory_worker_factory` | `Callable \| None` | 默认 `None` | 透传给 `MemoryRuntime` |

功能与内部调用：设置 `ready=False`、`error_code=None`、`stopping=threading.Event()`、`started=Future()`、`thread/loop/task=None`；延迟导入并创建 `self.memory = MemoryRuntime(settings, models, worker_factory=memory_worker_factory)`。

输出：`RunRuntime` 实例（`app.state.runtime`）。副作用：无线程启动。

### B2. `RunRuntime.start`

- 定位与签名：`async def start(self)`。
- 调用方与条件：lifespan 启动阶段。

| 输入 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `settings.db_url` | `str` | 配置 | 为空则直接返回 | 无数据库不启动执行器 |
| `settings.enable_runs` | `bool` | `MYBOT_API_RUNS` | `False` 则直接返回 | 仅浏览模式 |

功能与内部调用：
1. 条件不满足 → 直接返回（`ready` 保持 `False`）。
2. 启动 `threading.Thread(target=self._thread_main, name="mybot-runs", daemon=True)`。
3. `await asyncio.wrap_future(self.started)`：等待执行线程完成初始化或抛错。
4. `await self.memory.start()`；异常时记录“后台记忆暂不可用，对话服务继续运行”并 `await self.memory.close()`。

输出：无返回值。异常与边界：`_thread_main` 初始化失败会使 `started` 抛出 `RuntimeError("Agent runtime initialization failed")`，并置 `error_code="runtime_unavailable"`，lifespan 记录后启动失败。

### B3. `RunRuntime._thread_main`

- 定位与签名：`def _thread_main(self)`（线程入口，同步）。
- 调用方与条件：`start` 创建的 `mybot-runs` 线程。

功能：从 `server.__main__` 导入 `create_event_loop`（Windows 上强制 Selector），在 `asyncio.Runner(loop_factory=create_event_loop)` 中运行 `self._serve()`。捕获 `BaseException`：`ready=False`、`error_code="runtime_unavailable"`、记录“对话执行器异常停止”；若 `started` 未完成则 `set_exception`。

### B4. `RunRuntime._serve`：资源初始化与启动恢复

- 定位与签名：`async def _serve(self)`。
- 调用方与条件：执行线程的事件循环内；`_serve` 正常结束时执行线程退出。

初始化步骤（`database.open()` 后）：

| 步骤 | 操作 | 说明 |
| --- | --- | --- |
| 1 | `self.loop = asyncio.get_running_loop()`；`self.task = asyncio.current_task()` | 供 `close()` 从外部线程取消 |
| 2 | `database = Database(self.settings)`；`await database.open()` | 执行线程独立连接池（与 HTTP 池分开） |
| 3 | `pg_try_advisory_lock(hashtextextended('mybot-ui-runtime', 0))` | 同一数据库只允许一个服务执行器；未取得则抛 `RuntimeError("Another service owns the run worker")` |
| 4 | `AsyncPostgresSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=None))` + `await checkpointer.setup()` | 检查点写入复用所有权连接，受限反序列化 |
| 5 | `RunRepository(pool, connection=conn, connection_lock=checkpointer.lock)`、`SpeechRepository(pool, connection=conn, connection_lock=checkpointer.lock)` | 运行与检查点共用同一连接和串行锁；失去所有权连接后不能换连接继续提交 |
| 6 | `adapter = (adapter_factory or AgentAdapter)(pool, checkpointer, catalog, models, stopping=stopping)` | 注入停止事件 |
| 7 | `speech = (speech_factory or SpeechService)(settings.audio_root, catalog, models)` | 语音合成服务 |
| 8 | `await speech_repo.recover()` | 遗留 `running` 语音任务标记 `interrupted`（`error_code='service_interrupted'`） |
| 9 | 启动恢复：`for run in await repo.recovery_runs():` → `await adapter.repair(run, repo)`（失败记录“启动时检查点恢复延后”并继续）→ `await repo.finish(run["id"], error="service_interrupted", interrupted=True, needs_recovery=not repaired)` | 遗留 `running` 运行标记中断；修复成功清除 `recovery_run_id`，失败保留供下次 |
| 10 | `self.ready = True`；`self.started.set_result(None)` | 就绪后 `start()` 返回 |

恢复语义：`finish` 发现已提交正文时改为 `completed_with_warnings`（附加 `postprocessing_interrupted`），不重新生成正文；未提交正文则 `interrupted`，用户可显式重试。

### B5. `AgentAdapter.__init__` 与 `graph`

- 定位与签名：[server/services/agent.py](../../../server/services/agent.py) 的 `def __init__(self, pool, checkpointer, catalog, models, *, builder=None, stopping=None)` 与 `async def graph(self, run, **kwargs)`。
- 输入：`pool`（数据库连接池）、`checkpointer`（`AsyncPostgresSaver`）、`catalog`、`models`、`builder`（默认 `agent.builder.build_rp_agent`）、`stopping`（`threading.Event`）。
- 功能：`graph` 直接 `await self.builder(run["character_id"], pool=self.pool, checkpointer=self.checkpointer, **kwargs)`；服务每次运行构建一个新图（图内模型客户端按任务配置快照缓存）。
- 输出：编译后的 LangGraph 图对象。`kwargs` 由调用方决定（见 R4）。

### B6. `MemoryRuntime` 的创建（交叉引用）

`RunRuntime.__init__` 内 `from server.services.memory_runtime import MemoryRuntime` 并实例化。其 `start/_thread_main/_serve/close`、模型门锁与 `active_job` 详见 [memory 叶子](../memory/README.md)，本页不重复。

## 运行链

### R1. `_serve` 主循环（领取与执行）

- 定位与签名：[server/services/runtime.py](../../../server/services/runtime.py) 中 `_serve` 的 `while not self.stopping.is_set()` 循环。
- 调用方与条件：初始化与启动恢复完成后持续运行，直到 `stopping` 置位。

| 输入 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `self.gate` | `threading.Lock` | 构造时注入 | `acquire(blocking=False)` | 模型配置应用期间暂停领取；非阻塞避免阻塞事件循环 |
| `repo.claim()` | 协程 | `RunRepository` | 无队列返回 `None` | 领取对话运行 |
| `speech_repo.claim()` | 协程 | `SpeechRepository` | 无队列返回 `None` | 领取语音任务 |

功能与内部调用：
1. `if self.gate.acquire(blocking=False):` 后 `try`：
   - `run = await repo.claim()`；有则 `worked=True`，`await self._run_one(adapter, repo, run)`。
   - 否则 `job = await speech_repo.claim()`；有则记录“语音任务开始”，`await speech.execute(job)` → `await speech_repo.finish(job["id"])` → 记录“语音任务完成”。
   - `except asyncio.CancelledError`：`await speech_repo.recover()` 后重新抛出（关闭路径）。
   - `except Exception`：记录“语音任务失败”，`await speech_repo.finish(job["id"], error="speech_failed")`。
   - `finally: self.gate.release()`。
2. 未取得门锁或没有可领取任务时 `await asyncio.sleep(0.2)`。
3. 循环结束（stopping）后进入 `finally`：`self.ready=False`；连接未关闭则 `pg_advisory_unlock('mybot-ui-runtime')`；外层 `finally` 关闭执行线程连接池。

输出：无返回值；副作用为数据库状态迁移与日志。异常：`CancelledError` 在 `_serve` 外层被静默（正常关闭）；其他初始化异常向上导致线程退出。

### R2. `RunRuntime._run_one`

- 定位与签名：`async def _run_one(self, adapter, repo, run)`。
- 调用方与条件：主循环领取到对话运行后。
- 功能：`with logging_context(run_id=run["id"], thread_id=run["thread_id"], character=run["character_id"]):` 包装后调用 `await self._execute_run(adapter, repo, run)`，使本轮日志自动带标识。
- 输出：无返回值（`_execute_run` 的结果体现在运行状态与事件）。

### R3. `RunRuntime._execute_run`

- 定位与签名：`async def _execute_run(self, adapter, repo, run)`。
- 输入：`run` 为 `repo.claim()` 返回的上下文行，含 `id/thread_id/character_id/graph_thread_id/user_message_id/text/input_created_at/status/retry_of/base_state/import_state/recovery_run_id/memory_*` 等字段（见 [conversations 叶子](../conversations/README.md) 的 `context`）。

功能：
1. `started = perf_counter()`；`await adapter.execute(run, repo)`。
2. `except BaseException as exc`（包含关闭时的取消）：
   - `current = await repo.recovery_context(run["id"])`，`await adapter.repair(current, repo)`；修复失败记录“检查点修复延后”。
   - `await repo.finish(run["id"], error="service_interrupted" if isinstance(exc, asyncio.CancelledError) else "agent_failed", interrupted=isinstance(exc, asyncio.CancelledError), needs_recovery=not repaired)`。
   - `CancelledError` 重新抛出；其他异常记录“对话执行失败”。
3. `finally`：记录“对话执行结束”及耗时。

输出：无返回值。状态变化：运行终态、`recovery_run_id`、检查点修复。关键约束：绝不因异常重做已提交正文（`finish` 的已提交分支）。

### R4. `AgentAdapter.execute`（单轮主链）

- 定位与签名：[server/services/agent.py](../../../server/services/agent.py) 的 `async def execute(self, run, repo)`。
- 调用方与条件：`_execute_run`；每个领取到的对话运行执行一次。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `run["character_id"]` | `str` | `claim`/`context` | 已注册角色 | 构建图、读取档案、记忆流键 |
| `run["graph_thread_id"]` | `str` | `threads` | `ui:<uuid>` | LangGraph 线程 ID |
| `run["user_message_id"]` | `UUID` | `runs` | 非空 | 生成输入消息 ID `user_{id}` |
| `run["text"]`、`run["input_created_at"]` | `str`、`datetime` | `messages` | 非空 | 组装 `<timestamp>` 前缀输入 |
| `run["memory_retrieval_enabled"]` / `memory_storage_enabled` | `bool` | `runs` 策略快照 | 默认取会话策略 | 决定图内工具与记忆任务 |
| `run["memory_policy_version"]` | `int` | `runs` | ≥1 | 记忆源与撤权核对 |
| `run["retry_of"]`、`run["base_state"]` | `UUID \| None`、`dict \| None` | `runs` | 重试时才非空 | 恢复上次运行前状态 |
| `run["recovery_run_id"]`、`run["import_state"]` | `UUID \| None`、`dict \| None` | `threads` | 按需 | 修复上一轮、导入种子 |

隐式输入：`self.models.snapshot()`（生效配置与版本）、`self.catalog.get()`（角色档案）、`self.stopping`、`repo` 的数据库连接。

功能与内部调用：
1. `from config.model_config import model_config_scope`；`data, model_version = self.models.snapshot()`；`profile = self.catalog.get(run["character_id"])`；`text = profile.profiles.get("zh") or next(iter(profile.profiles.values()))`（缺 `zh` 时取任意档案）。
2. 定义并传入三个回调（R4.1~R4.3）。
3. `with model_config_scope(data):` 内构建图：`graph = await self.graph(run, character_profile=text, before_node=before, on_commit=commit, memory_retrieval_enabled=..., memory_storage_enabled=..., memory_source=memory_source)`；配置 `config = {"configurable": {"thread_id": run["graph_thread_id"]}, "recursion_limit": 200}`。
4. 恢复/重试前置：
   - `run["recovery_run_id"]` 非空 → `previous = await repo.recovery_context(run["recovery_run_id"])`；`await self.repair(previous, repo)`。
   - `base = (await graph.aget_state(config)).values or {}`；若为空且 `run["import_state"]` 存在 → `await graph.aupdate_state(config, thaw_state(run["import_state"]), as_node="reply_failed")` 后重读。
   - `run["retry_of"] and run["base_state"] is not None` → `base = thaw_state(run["base_state"])`（重试从上次运行前状态开始）。
5. `await repo.save_base(run["id"], freeze_state(base), model_version, profile.version)`；同步 `run["base_state"]`；`await repo.publish(run["id"], "state.updated", {"retrieved_memories": []})`（清空上一轮检索展示）。
6. 组装 `inputs`（R4.4）。
7. 策略版本变化时重置记忆通道（R4.5）。
8. `async for update in graph.astream(inputs, config, stream_mode="updates", durability="sync"):`（R4.6）逐节点投影公开事件。
9. `final = (await graph.aget_state(config)).values or {}`；`error = "reply_failed" if final.get("reply_error") else None`；有错误先 `await self.repair(run, repo)`；最后 `await repo.finish(run["id"], error=error, warning="memory_delayed" if final.get("memory_warning") else None)`。

输出：无返回值；`repo.finish` 写入终态与 `run.completed/run.failed` 事件。

#### R4.1 `before(name, state)` 回调

- 定位与签名：`execute` 内 `async def before(name, state)`；由 `agent/builder.py` 的 `observed` 包装在**每个节点执行前**调用。
- 输入：`name`（图节点名）、`state`（当前图状态）。

功能：
1. `self.stopping.is_set()` → `raise asyncio.CancelledError()`（停止时不进入下一节点）。
2. `async with repo.connection() as conn: await conn.execute("SELECT 1")`：在进入节点前确认所有权连接仍存活（失去 advisory lock 会话立即失败）。
3. `phase = _PHASES.get(name)`；`name == "tools"` 时看 `state["messages"][-1].tool_calls`：包含 `memory_query` → `recalling`，否则 `using_tools`。
4. 有 phase → `await repo.publish(run["id"], "phase", {"phase": phase})`。

`_PHASES` 映射（模块级常量）：`begin_turn→preparing`、`draft→replying`、`check→reviewing`、`world_state_update/participant_state_in/participant_state_out→updating_state`、`prepare_memory→updating_memory`。`tools` 与其余节点不在映射中则不发送 phase（`limit_context`、`apply_memory_results`、`commit_reply`、`reply_failed`、`update_iter`、`tts`、`enqueue_memory` 等）。

输出：无返回值；副作用为 phase 事件与运行 `phase` 列更新。

#### R4.2 `commit(state, output)` 回调

- 定位与签名：`execute` 内 `async def commit(state, output)`；由 `observed` 仅在 `name == "commit_reply"` 时、节点返回后立即调用（早于图写检查点）。
- 输入：`state["draft_reply"]`（检查通过的正文）、`output["messages"][0]`（提交节点返回的 AI 消息）。

功能：
1. `message = output["messages"][0]`；若 `message.id != f"reply_{run['id']}"` → `raise RuntimeError("Unexpected graph reply ID")`。
2. `await repo.commit_reply(run["id"], state["draft_reply"].strip(), message.id)`。

**顺序保证**：`agent/builder.py` 的 `observed` 先 `await on_commit(...)` 再返回节点结果，因此“正文与 `message.committed` 同事务落库”发生在 LangGraph 写入检查点之前；落库失败会让节点失败，图不会带着未持久化的正文推进。

#### R4.3 `memory_source(state)` 回调

- 定位与签名：`execute` 内 `async def memory_source(state)`；由 `prepare_memory` 节点调用。
- 功能：`return await repo.memory_messages(run["thread_id"], run["memory_policy_version"], state.get("memory_processed_through"))`——只取与运行策略版本一致、且 `memory_storage_enabled` 为真的正式消息（含被截断运行保留的用户消息的 `detached_*` 策略），返回带 `<timestamp>` 前缀的 LangChain 消息列表。SQL 语义见 [conversations 叶子](../conversations/README.md)。

#### R4.4 `inputs` 组装

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `messages` | `[_input(run)]` | 本轮用户输入 |
| `service_run_id` | `str(run["id"])` | 图内标识本轮服务运行，供修复核对 |
| `need_tts` | `False` | 服务侧不使用图内 TTS 播放 |
| `need_event_judge` | `run['memory_storage_enabled']` | 不存储时跳过事件判断与记忆整理 |
| `memory_retrieval_enabled` / `memory_storage_enabled` | 运行策略快照 | 图内工具与记忆节点开关 |
| `memory_policy_version` | `run['memory_policy_version']` | 快照版本 |

`_input(run)`（模块级函数）：返回 `HumanMessage(id=f"user_{run['user_message_id']}", content=f"<timestamp>{run['input_created_at'].isoformat()}</timestamp>\n" + run["text"])`。时间戳只作为模型上下文前缀，不写回消息正文。

#### R4.5 策略版本重置

当 `base.get('memory_policy_version', 1) != run['memory_policy_version']` 时，`inputs` 追加重置：`memory_pending_job=None`、`memory_active_job=None`、`memory_warning=''`、`memory_processed_through=''`、`memory_processed_fingerprint=''`、`memory_submitted_through=''`、`memory_trimmed_through=''`、`iteration=0`，并把 `memory_last_applied_job` 设为该角色/线程在 `memory_service.results` 的最大 `job_id`（表不存在时为 0）。目的：策略变更后不复用旧授权版本的进度标记。`memory_storage_enabled` 为假时另清空 `memory_pending_job/memory_active_job/memory_warning`。

#### R4.6 `astream` 事件投影

`graph.astream(..., stream_mode="updates", durability="sync")` 的每个 `(name, delta)`：
- `delta` 不是 dict 跳过。
- `state = public_state(delta)`；非空则 `await repo.publish(run["id"], "state.updated", state)`。
- `name == "tools"`：遍历 `delta["messages"]`，仅处理 `name == "memory_query"` 的工具消息；`MemorySearchResult.model_validate_json(message.content)` 成功且 `status in ("ok","empty")` 时 `publish memory.retrieved`，命中字段为 `id/memory/event_date/update_time`。解析失败静默跳过。

`public_state(values)`（模块级函数）：只投影白名单字段——`character_state` 取 `location/mood/body/clothing/hearing`，`user_state` 取 `location/mood/body/clothing`，`world_state` 取 `weather` 与 `time.date/time.weekday/time.period`；值非 `str` 时置 `None`。不包含候选、检查反馈、推理或全量状态。

#### R4.7 终态

- `reply_error` 非空 → 先 `repair`（把检查点复位到可重试状态），`repo.finish(error="reply_failed")` → 未提交正文则运行 `failed`。
- `memory_warning` 非空 → `warning="memory_delayed"`；已提交正文时终态为 `completed_with_warnings`。

### R5. `AgentAdapter.repair`

- 定位与签名：`async def repair(self, run, repo)`。
- 调用方与条件：`_serve` 启动恢复、`execute` 中 `recovery_run_id` 前序运行、`execute` 失败终态、`_execute_run` 异常路径；`run` 为运行上下文行。

功能与内部调用：
1. 以 `recovery_only=True` 构建图（`self.graph(run, recovery_only=True)`；builder 用空实现注册全部节点，不加载模型、工具、记忆存储，不调用挂起节点）。
2. `config = {"configurable": {"thread_id": run["graph_thread_id"]}}`；`current = await graph.aget_state(config)`；`values = dict(current.values or {})`；`base = thaw_state(run["base_state"])`；`snapshot = await repo.snapshot(run["id"])`。
3. `committed` 取快照消息中第一条 `role == "assistant"`；`expected = f"reply_{run['id']}"`；`graph_reply` 取检查点消息中 id 等于 `expected` 的消息。
4. 防御性恢复（旧版本适配器可能已写检查点未落库）：`committed is None and graph_reply is not None and run["status"] == "running"` 时解析 `graph_reply.content` 的 `<timestamp>...</timestamp>\n` 前缀；格式不符 → 409 `checkpoint_conflict`；否则 `committed = await repo.commit_reply(run["id"], text, expected)`。
5. `run["base_state"] is None`（Worker 尚未进入图）：`await repo.clear_recovery(run["thread_id"])` 后直接返回，保留现有检查点。
6. `safe = base`；有已提交正文时 `safe = values if values.get("service_run_id") == str(run["id"]) else base`，补上缺失的 `user_{user_message_id}` 输入消息与 `AIMessage(id=expected, content="<timestamp>...")`；若 `service_reply_counted` 不为真，`iteration = base.iteration + 1` 并置 `service_reply_counted=True`。
7. 组装 `reset` 字典：清空草稿/检查/重试/记忆/状态/迭代等瞬态通道（`draft_reply=""`、`draft_status` 按是否已提交取 `committed/pending`、`check_rounds=0`、`reply_error=""`、`memory_*` 清空、`world_state/character_state/user_state={}`、`iteration=0` 等），并覆盖为保留值；消息用 `[RemoveMessage(id=REMOVE_ALL_MESSAGES), *safe.get("messages", [])]` 整体替换。
8. `await graph.aupdate_state(config, None, as_node=END)`（丢弃挂起任务）→ `await graph.aupdate_state(config, reset, as_node="reply_failed")`。
9. 复核：`if (await graph.aget_state(config)).next: raise RuntimeError("Checkpoint repair left pending tasks")`。
10. `async with repo.connection() as conn:` 执行 `UPDATE mybot_ui.threads SET state = state || %s`（`public_state(reset)`，JSONB 合并）→ `await repo.clear_recovery(run["thread_id"])` → 记录“检查点修复完成”。

输出：无返回值。异常：格式冲突抛 409；修复后仍有挂起任务抛 `RuntimeError`，由调用方转为 `needs_recovery`。副作用：更新检查点与 `threads.state`。

### R6. `RunRuntime.close`

- 定位与签名：`async def close(self)`。
- 调用方与条件：lifespan 关闭阶段。

功能：
1. `self.ready=False`；`self.stopping.set()`。
2. `memory_close = asyncio.create_task(self.memory.close())`（先通知记忆停止，再等待原生推理结束）。
3. 执行线程仍存活时：`self.loop.call_soon_threadsafe(self.task.cancel)`；`await asyncio.to_thread(self.thread.join)`（原生推理不可安全强杀，等待其自然结束）。
4. `await memory_close`。

输出：无返回值。边界：退出等待超时由 uvicorn `timeout_graceful_shutdown=10` 与桌面托管逻辑处理；未完成语音在下一次启动标记 `interrupted`。

### R7. 辅助函数与常量

| 名称 | 签名/类型 | 功能 |
| --- | --- | --- |
| `public_state` | `def public_state(values)` | 白名单投影图状态（R4.6） |
| `freeze_state` | `def freeze_state(values)` | `messages_to_dict` 后 `json.loads(json.dumps(result, default=str))`，得到可写 JSONB 的快照 |
| `thaw_state` | `def thaw_state(values)` | `deepcopy` 后用 `messages_from_dict` 还原 `messages`；空输入返回 `{"messages": []}` |
| `_input` | `def _input(run)` | 构造带时间戳前缀的 `HumanMessage`（R4.4） |
| `_PHASES` | `dict[str,str]` | 节点名 → 公开 phase 映射（R4.1） |
| `_STATE_FIELDS` | `set[str]` | `{"world_state","character_state","user_state"}`；当前模块内无引用，未参与执行链（仅静态定义） |

## 分支与异常链

| 条件 | 处理 | 终点 |
| --- | --- | --- |
| 未配置数据库或 `MYBOT_API_RUNS=0` | `start` 直接返回，不建线程 | 仅浏览模式，`/ready` 非就绪 |
| 已有其他服务执行器持有 advisory lock | 抛 `RuntimeError("Another service owns the run worker")` | 执行线程退出、启动失败 |
| 记忆初始化失败 | 记录并关闭记忆；对话继续 | `GET /service` 报 `memory_runtime_ready=false` |
| 停止事件置位（关闭中） | `before` 抛 `CancelledError`；`_execute_run` 以 `interrupted` 收尾并重新抛出 | 关闭流程继续等待线程 |
| 执行中所有权连接断开 | `before` 的 `SELECT 1` 失败 → 节点异常 → `repair` + `finish(agent_failed)` | 运行失败，保留 `needs_recovery` |
| 图返回 `reply_error` | `repair` 后 `finish(error="reply_failed")` | 无正式正文，`failed` |
| 已提交正文但后处理失败 | `finish` 转为 `completed_with_warnings` + `postprocessing_interrupted` | 不重生成正文 |
| 记忆整理延迟 | `warning="memory_delayed"` | 终态携带警告 |
| 语音合成失败 | `speech_repo.finish(error="speech_failed")` | 任务 `failed`，可换请求键重试 |
| 关闭时语音执行中 | `CancelledError` → `speech_repo.recover()` | 任务回 `interrupted` |
| 启动时遗留 `running` 运行 | `repair`（失败则延后）→ `finish(interrupted)` | `interrupted`，用户可显式重试 |

## 输入输出示例

R4 输入组装（虚构 ID，`memory_storage_enabled=false`）：

```python
inputs = {
    "messages": [HumanMessage(id="user_8f2c...", content="<timestamp>2026-09-23T10:00:00+08:00</timestamp>\n晚上好。")],
    "service_run_id": "3f8c1c2e-...",
    "need_tts": False,
    "need_event_judge": False,
    "memory_retrieval_enabled": True,
    "memory_storage_enabled": False,
    "memory_policy_version": 3,
    "memory_pending_job": None, "memory_active_job": None, "memory_warning": "",
}
```

R4.6 投影（`world_state` 增量 → `state.updated` payload）：

```json
{"world_state": {"weather": "晴", "time": {"date": "2026-09-23", "weekday": "周三", "period": "晚上"}}}
```

R5 修复结果：检查点被复位为 `reply_failed` 结束态（`next` 为空），`threads.state` 合并 `public_state(reset)`，`recovery_run_id` 清空。

## 关联文档与验证依据

- 上级：[服务总览](../README.md)；系统总览：[README/README.md](../../README.md)
- 同级：[conversations](../conversations/README.md)（队列、快照、commit/finish 语义）· [speech](../speech/README.md) · [memory](../memory/README.md) · [settings](../settings/README.md)（门锁与生效版本）· [http](../http/README.md)（lifespan 装配）
- Agent 侧：[builder/README.md](../../agent/builder/README.md)（`observed` 回调注入顺序）· [node/tts/README.md](../../agent/node/tts/README.md) · [classes/README.md](../../agent/classes/README.md)
- 配置：[config/README.md](../../config/README.md)（`model_config_scope`、节点模型缓存）
- 测试依据：[tests/server/test_execution.py](../../../tests/server/test_execution.py)（真实 LangGraph 受控节点、提交两侧故障、状态投影）、[tests/server/controlled.py](../../../tests/server/controlled.py)、[tests/server/test_memory_runtime.py](../../../tests/server/test_memory_runtime.py)
- 迁移自原 `server/README.md`（原文件已移除）的边界说明：执行器使用持久数据库会话锁；运行写入与 checkpoint 共用该连接和串行锁，防止丢失所有权后换连接继续提交；私有运行前快照仅供恢复，不出现在 API DTO；恢复过程清除挂起图任务，不自动重放未知是否完成的外部副作用，因此不承诺跨数据库/外部模型的 exactly-once。
- 验证记录（迁移自原文档，未在本次编写中重跑）：2026-09-21 记录包含 3 项真实图/TTS 节点/状态投影测试通过；2026-09-22 记录后台记忆分离后 52 项服务测试通过，覆盖受控记忆阻塞时下一轮对话仍完成、记忆失败/初始化失败不影响对话、停止后任务保留及重启消费。
