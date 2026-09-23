# HTTP 层：应用工厂、访问边界与路由

## 职责与入口

本页覆盖 `server/` 的 HTTP 装配与协议边界，源码入口如下：

| 文件 | 类型 | 职责 |
| --- | --- | --- |
| [server/app.py](../../../server/app.py) | 应用工厂 | `create_app`：组装 lifespan、中间件、异常处理器与 9 个路由模块 |
| [server/__main__.py](../../../server/__main__.py) | 进程入口 | `python -m server`：Windows Selector 事件循环、单 worker uvicorn |
| [server/config.py](../../../server/config.py) | 配置 | `ServiceSettings`：端口、DB、记忆 schema、运行开关、owner token |
| [server/routes/](../../../server/routes/) | 路由 | 9 个模块共 33 路径 / 37 操作 |
| [server/classes/api.py](../../../server/classes/api.py) | 响应协议 | `ServiceError`、`ErrorResponse`、`HealthStatus`、`ReadyStatus`、`SettingsStatus` 等 |
| [server/classes/runs.py](../../../server/classes/runs.py) | 请求/运行协议 | 会话、运行、消息、事件、语音 DTO |
| [server/classes/settings.py](../../../server/classes/settings.py) | 设置协议 | `PUBLIC_NODES`、`ProfileWrite`、模型设置写请求 |
| [server/services/access.py](../../../server/services/access.py) | 中间件 | `LocalAccessMiddleware`：Origin 白名单与写请求头 |
| [server/services/files.py](../../../server/services/files.py) | 工具 | `atomic_write` 原子写文件 |
| [server/services/health.py](../../../server/services/health.py) | 服务 | `readiness`：数据库就绪与能力 |
| [server/services/settings.py](../../../server/services/settings.py) | 服务 | `read_settings`：脱敏配置报告（详见 [settings 叶子](../settings/README.md)） |

调用方为桌面端、浏览器调试页面与 CLI HTTP 客户端；所有请求都是本机回环请求，写请求必须携带固定客户端头。

## 调用链总览

```text
构建阶段（进程启动，仅一次）
B1 ServiceSettings.from_environment() ── 读取 config/.env 与进程环境变量
B2 create_app(settings) ── 构造 FastAPI 实例
B3 lifespan(app) ── 打开数据库、启动 RunRuntime/MemoryRuntime 与 LegacyService
B4 中间件装配 ── LocalAccessMiddleware / CORS / TrustedHost
B5 异常处理器装配 ── ServiceError / RequestValidationError / HTTPException / DB / Exception
B6 include_router(prefix="/api") ── 挂载 9 个路由模块
B7 __main__.main() ── uvicorn.Config + asyncio.Runner(SelectorEventLoop)

运行阶段（每个 HTTP 请求）
R1 LocalAccessMiddleware.__call__ ── Origin / 写请求头校验
R2 路由处理函数 ── 见 R2.1~R2.9 端点表；调用 repositories/services
R3 routes/runs.py 的 SSE stream() ── 轮询 run_events 推送事件
R4 readiness() ── GET /ready、GET /settings 共用
R5 atomic_write() ── 档案与模型配置落盘（被 characters/settings 叶子调用）
```

构建关系（B）与运行关系（R）分离：工厂只组装对象，路由处理函数才读取请求并调用下层服务。

## 构建链

### B1. `ServiceSettings` 与 `from_environment`

- 定位与签名：[server/config.py](../../../server/config.py) 的 `@dataclass(frozen=True) class ServiceSettings` 与 `ServiceSettings.from_environment()`（同步类方法）。
- 调用方与条件：`create_app()` 未显式传入 settings 时调用 `from_environment`；`__main__.main()` 也调用一次以取端口并允许 `--port` 覆盖。

字段与默认值：

| 字段 | 类型 | 默认值 | 环境变量与含义 |
| --- | --- | --- | --- |
| `db_url` | `str`（`repr=False`） | `""` | `DB_URL`：现有 Postgres 连接串；为空表示不启用数据库接口 |
| `character_root` | `Path` | `<项目根>/Character` | 固定路径，不来自环境变量 |
| `model_config` | `Path` | `<项目根>/config/models.yaml` | 固定路径 |
| `environment` | `dict[str,str]`（`repr=False`） | `{}` | 合并后的 dotenv + 进程环境，用于 `read_settings` 判断凭据是否存在 |
| `port` | `int` | `8765` | `MYBOT_API_PORT`；命令行 `--port` 优先 |
| `db_timeout` | `float` | `5.0` | `MYBOT_API_DB_TIMEOUT`：连接/借用超时秒数 |
| `memory_schema` | `str` | `"public"` | `MYBOT_MEMORY_SCHEMA`：现有角色记忆父表所在 schema，也是连接 `search_path` 的第一段 |
| `enable_runs` | `bool` | `True` | `MYBOT_API_RUNS == "1"`：启用对话/语音后台队列 |
| `memory_worker` | `bool` | `True` | `MYBOT_API_MEMORY_WORKER == "1"`：启用独立记忆消费线程 |
| `audio_root` | `Path` | `<项目根>/data/audio` | 固定路径，TTS 输出目录 |
| `owner_token` | `str`（`repr=False`） | `""` | `MYBOT_SERVICE_OWNER_TOKEN`：桌面托管启动时注入的退出凭据 |

- `__post_init__` 校验：`1 <= port <= 65535`；`0 < db_timeout <= 60`；`memory_schema` 非空且不含 `\x00`；违反时抛 `ValueError`。
- `from_environment`：先用 `dotenv_values(<项目根>/config/.env)` 取值（过滤 `None`），再 `values.update(os.environ)`，因此**进程环境变量优先**；字符串 `"1"` 才视为启用开关。
- `allowed_origins` 属性返回固定 7 项元组：`http://127.0.0.1:{port}`、`http://localhost:{port}`、`http://127.0.0.1:5173`、`http://localhost:5173`、`tauri://localhost`、`http://tauri.localhost`、`https://tauri.localhost`。仅依赖端口，不读额外配置。

输出：不可变 `ServiceSettings`，同时被 lifespan、中间件、各服务与路由通过 `app.state.settings` 读取。

异常与去向：`ValueError` 在 `create_app`/`__main__` 处向上抛出；`__main__` 捕获后 `parser.error("服务配置无效，请检查端口、超时和记忆 schema 配置。")` 并以退出码 2 结束。

### B2. `create_app`

- 定位与签名：[server/app.py](../../../server/app.py) 的 `create_app(settings: ServiceSettings | None = None, *, database_factory=Database, runtime_factory=RunRuntime) -> FastAPI`（同步）。
- 调用方与条件：`__main__.main()` 调用；测试通过 `database_factory`/`runtime_factory` 注入受控实现。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `settings` | `ServiceSettings \| None` | 默认 `None` | 为 `None` 时调用 `from_environment()` |
| `database_factory` | `Callable[[ServiceSettings], Database]` | `Database` | 测试替换数据库实现 |
| `runtime_factory` | `Callable[..., RunRuntime]` | `RunRuntime` | 测试替换运行执行器 |

功能与内部调用：
1. 生成 `FastAPI(title="mybot local API", version=server.__version__（当前 "0.1.0"）, lifespan=lifespan, ...)`，并在 OpenAPI 中声明 400/404/422/503 使用 `ErrorResponse`。
2. 依次 `add_middleware(LocalAccessMiddleware)`、`add_middleware(CORSMiddleware)`、`add_middleware(TrustedHostMiddleware)`（见 B4 的执行顺序）。
3. 注册异常处理器（B5）。
4. 按固定顺序 `include_router(router, prefix="/api")`：`health, characters, memories, settings, threads, runs, speech, service, legacy`（B6）。
5. 返回 app；`lifespan` 在 uvicorn 启动/关闭时执行。

输出：`FastAPI` 实例。副作用：注册中间件与异常处理器；真正资源在 lifespan 中创建。

### B3. `lifespan`：资源所有权与顺序

- 定位与签名：[server/app.py](../../../server/app.py) 内 `@asynccontextmanager async def lifespan(app)`，由 FastAPI 在 startup/shutdown 各调用一次。
- 调用方与条件：uvicorn 启动服务时进入，`yield` 前为启动，`yield` 后（含异常）为关闭。

初始化顺序（每个对象都挂到 `app.state`）：

| 顺序 | 对象 | 构造参数 | 说明 |
| --- | --- | --- | --- |
| 1 | `database` | `database_factory(settings)` | `app.state.database` |
| 2 | `catalog` | `CharacterCatalog(settings.character_root)` | 角色目录注册表，见 [characters 叶子](../characters/README.md) |
| 3 | `gate` | `threading.Lock()` | 对话/语音执行器共享门锁，传给 models 与 runtime |
| 4 | `models` | `ModelSettingsService(settings, gate)` | 保存/生效模型配置，内部另建 `memory_gate` |
| 5 | `runtime` | `runtime_factory(settings, catalog, models, gate)` | `RunRuntime`，内部创建 `MemoryRuntime`，见 [runtime 叶子](../runtime/README.md) |
| 6 | `legacy` | `LegacyService(database, catalog, settings.audio_root)` | 旧 CLI 导入循环，见 [legacy 叶子](../legacy/README.md) |
| 7 | `checkpoint_sync` | `CheckpointSyncService(database)` | 检查点截断服务，见 [conversations 叶子](../conversations/README.md) |

启动调用顺序：记录日志“后端启动”后依次 `await asyncio.to_thread(catalog.load)`（文件扫描放线程）→ `await database.open()`（无 `db_url` 时直接返回）→ `await runtime.start()`（内部再启动 `MemoryRuntime`，记忆失败不阻断对话）→ `await legacy.start()`（仅当连接池存在时创建循环任务）→ 记录“后端就绪”（带 `runtime_ready`、`memory_ready`）。

关闭顺序（`finally`）：记录“后端停止中” → `await legacy.close()`（取消导入循环任务）→ `await runtime.close()`（先取消记忆线程再等待执行线程）→ 内层 `finally` 中 `await database.close()` → 记录“后端已停止”。

异常与降级：
- 未配置数据库：`database.open()` 直接返回，`runtime.start()` 因 `not db_url or not enable_runs` 直接返回（`runtime.ready=False`），`legacy.start()` 不创建任务；角色/图片/模型配置/档案写回仍可用，数据库接口返回 503，`GET /ready` 为 `not_ready`。
- 已配置数据库但连接或迁移失败：异常从 lifespan 抛出并记录“后端生命周期异常”，uvicorn 启动失败（服务拒绝启动）。
- `runtime.start()` 中记忆初始化失败只记录“后台记忆暂不可用，对话服务继续运行”并 `await self.memory.close()`；服务级状态通过 `GET /service` 暴露。

### B4. 中间件装配与执行顺序

装配调用（`create_app` 内，按加入顺序列出）：

| 加入顺序 | 中间件 | 参数 | 作用 |
| --- | --- | --- | --- |
| 1 | `LocalAccessMiddleware` | `allowed_origins=settings.allowed_origins` | Origin 白名单 + 写请求头，见 R1 |
| 2 | `CORSMiddleware` | `allow_origins=list(settings.allowed_origins)`、`allow_methods=["GET","HEAD","POST","PUT","PATCH","DELETE","OPTIONS"]`、`allow_headers=["Content-Type","X-Mybot-Client","Last-Event-ID","X-Mybot-Owner"]`、`allow_credentials=False` | 浏览器跨源与预检响应 |
| 3 | `TrustedHostMiddleware` | `allowed_hosts=["127.0.0.1","localhost"]`、`www_redirect=False` | 拒绝其他 Host |

按 Starlette 的中间件栈规则，**后加入者位于外层**，因此请求实际经过：`ServerErrorMiddleware → TrustedHost → CORS → LocalAccessMiddleware → ExceptionMiddleware → 路由`。含义：Host 不合法先被拒绝；CORS 预检可在 LocalAccess 之前直接响应；普通请求最终仍必须通过 LocalAccess 的 Origin 与写请求头校验。`GET/HEAD/OPTIONS` 不要求 `X-Mybot-Client`，无 Origin 的本机脚本可以调用。

### B5. 异常处理器与错误信封

| 处理器 | 捕获 | 返回 |
| --- | --- | --- |
| `service_error` | `ServiceError` | 记录“业务请求失败”；`_error(exc.code, exc.message, exc.status_code, exc.details)` |
| `validation_error` | `RequestValidationError` | 固定 `invalid_request` / “请求参数无效，请检查类型、分页范围和长度。” / 422，**不回显输入** |
| `http_error` | `HTTPException` | 404 → `not_found` / “接口不存在。”；其余 → `http_error` / “请求无法处理。”，沿用原状态码 |
| `database_error` | `psycopg.Error`、`psycopg_pool.PoolTimeout` | 记录“数据库请求失败”；`database_unavailable` / 503 |
| `unexpected_error` | `Exception`（ServerErrorMiddleware 兜底） | 记录“未处理的服务异常”；`internal_error` / 500 |

`_error(code, message, status, details)` 统一输出 `{"error":{"code":...,"message":...,"details":{...}}}`；`details` 仅在非空时出现（如 `thread_busy` 附 `run_id`）。错误文本不回传密钥、连接串或磁盘路径。

### B6. 路由装配

9 个模块按 `health, characters, memories, settings, threads, runs, speech, service, legacy` 顺序挂载到 `prefix="/api"`。各模块自带 `tags` 或 `prefix`（`memories` 为 `/characters/{character_id}/memories`，`threads` 为 `/threads`，`service` 为 `/service`）。OpenAPI 与 Swagger UI 由 FastAPI 在 `/docs`、`/openapi.json` 暴露。

### B7. `__main__.main` 与 `create_event_loop`

- 定位与签名：[server/__main__.py](../../../server/__main__.py) 的 `create_event_loop()` 与 `main()`（同步）。
- 调用方与条件：`python -m server`；Tauri 托管启动同样使用该入口。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `--port` | `int \| None` | 默认 `None` | 命令行覆盖 `MYBOT_API_PORT` |

功能与内部调用：
1. `argparse` 解析 `--port`；`ServiceSettings.from_environment()` 后 `dataclasses.replace(settings, port=args.port)`。
2. 延迟导入 `uvicorn`，调用 `utils.daily_logger.configure_backend_logging()` 初始化 `data/log/YYYY-MM-DD.log`，再导入 `create_app`。
3. 构造 `uvicorn.Config(create_app(settings), host="127.0.0.1", port=settings.port, workers=1, reload=False, proxy_headers=False, server_header=False, timeout_graceful_shutdown=10, log_config=None, access_log=False)`：仅回环、单进程、无 reload、优雅退出等待 10 秒、不输出访问日志。
4. `config.app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)`：供 `POST /api/service/shutdown` 触发退出。
5. `with asyncio.Runner(loop_factory=create_event_loop) as runner: runner.run(server.serve())`。`create_event_loop` 在 Windows 强制 `asyncio.SelectorEventLoop`（Uvicorn 默认可能选 Proactor，与 psycopg 异步不兼容），其他平台用 `asyncio.new_event_loop()`。
6. `KeyboardInterrupt` 静默；`if not server.started: raise SystemExit(1)`。

输出：进程运行/退出码。副作用：写启动日志、监听 `127.0.0.1:port`。

## 运行链

### R1. `LocalAccessMiddleware.__call__`

- 定位与签名：[server/services/access.py](../../../server/services/access.py) 的 `async def __call__(self, scope, receive, send)`（ASGI，异步）。
- 调用方与条件：每个 HTTP 请求，位于 CORS 内侧、路由之前；非 `http` scope（如 lifespan）直接放行。

| 输入 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `scope["method"]` | `str` | ASGI | 必有 | 判断是否写方法 |
| `Origin` 头（可多个） | `list[str]` | 请求头 | 无 Origin 表示本机非浏览器调用 | 来源白名单 |
| `X-Mybot-Client` | `str \| None` | 请求头 | 写请求必填 `mybot-desktop` | 阻止任意网页跨域写 |

功能与内部调用：
1. 取全部 `Origin` 头：数量 > 1，或仅有一个且不在 `allowed_origins` → 直接返回 403 `origin_forbidden`。
2. 方法不在 `GET/HEAD/OPTIONS` 且 `x-mybot-client != "mybot-desktop"` → 直接返回 403 `client_header_required`。
3. 其余交给内层 app。

输出：403 JSON 或继续调用。异常与边界：本中间件不抛异常；通过 `JSONResponse(scope, receive, send)` 自行发送响应。

### R2. 路由处理函数与端点表

各模块共用的取用方式：`request.app.state.{database,catalog,models,runtime,legacy,checkpoint_sync,settings}`。`routes/threads.repository(request)` 与 `routes/memories._repository(request, character_id)` 在连接池为空时抛 503 `database_unavailable`；`routes/runs.require_runtime(request)` 在 `runtime.ready` 为假时抛 503 `runtime_unavailable`，并调用 `models.snapshot()` 确保模型配置已就绪。

#### R2.1 health

| 方法 | 路径 | 处理函数 | 下游调用 |
| --- | --- | --- | --- |
| GET | `/api/health` | `health()` | 返回 `HealthStatus(version=server.__version__)` |
| GET | `/api/ready` | `ready(request, response)` | `readiness()`（R4）；按 `database/runtime.ready/models.active` 计算 `capabilities.chat/speech`；非 ready 时置 503 |

#### R2.2 characters

| 方法 | 路径 | 处理函数 | 下游调用 |
| --- | --- | --- | --- |
| GET | `/api/characters` | `characters(request)` | `catalog.summaries()` |
| GET | `/api/characters/{character_id}` | `character(...)` | `catalog.get()` |
| PUT | `/api/characters/{character_id}/profiles/{language}` | `write_profile(...)` | `catalog.write_profile(character_id, language, body.text, body.expected_version)` |
| GET | `/api/resources/{resource_id}` | `resource(...)` | `catalog.resource()` → `FileResponse(path, media_type, headers={"X-Content-Type-Options":"nosniff","Cache-Control":"no-cache"})` |

#### R2.3 memories

| 方法 | 路径 | 处理函数 | 下游调用 |
| --- | --- | --- | --- |
| GET | `/api/characters/{character_id}/memories` | `memories(...)` | `_repository` → `MemoryRepository.list(character_id, query, cursor, limit)` |
| GET | `/api/characters/{character_id}/memories/{memory_id}` | `memory(...)` | `_repository` → `MemoryRepository.get(character_id, memory_id)` |

查询参数约束：`query` ≤ 1000 字符（默认 `""`）、`cursor` ≤ 2048 字符、`limit` 1..100（默认 20）、`memory_id` 1..2^63-1。`_repository` 先 `catalog.get(character_id)`（未知角色 404），再要求 `database.pool` 非空。

#### R2.4 threads

| 方法 | 路径 | 处理函数 | 下游调用 |
| --- | --- | --- | --- |
| GET | `/api/threads` | `threads(...)` | `ThreadRepository.list_threads(cursor, limit, deleted)` |
| POST | `/api/threads` | `create_thread(body, request)` | `catalog.get(character_id)` → `ThreadRepository.create_thread(**body.model_dump())`，201 |
| GET | `/api/threads/{thread_id}` | `detail(...)` | `ThreadRepository.get_thread()` |
| PATCH | `/api/threads/{thread_id}` | `update(...)` | 无变更字段时抛 `empty_update`；否则 `ThreadRepository.update(thread_id, expected_version, changes)` |
| DELETE | `/api/threads/{thread_id}` | `delete(...)` | `ThreadRepository.trash(thread_id, expected_version)` |
| POST | `/api/threads/{thread_id}/restore` | `restore(...)` | `ThreadRepository.trash(..., restore=True)` |
| DELETE | `/api/threads/{thread_id}/purge` | `purge(...)` | `ThreadRepository.purge()`，返回 `{"deleted": True}` |
| GET | `/api/threads/{thread_id}/messages` | `messages(...)` | `ThreadRepository.messages(thread_id, before, limit)` |
| GET | `/api/threads/{thread_id}/state` | `state(...)` | `get_thread()` 后返回 `thread["state"]` |
| POST | `/api/threads/{thread_id}/checkpoint-sync` | `checkpoint_sync(...)` | `CheckpointSyncService.sync(thread_id, body)` |
| GET | `/api/threads/{thread_id}/memory-status` | `memory_status(...)` | `ThreadRepository.memory_status(thread_id, runtime.memory.active_job)` |

`PATCH` 请求体通过 `model_dump(exclude_none=True, exclude={'expected_version'})` 取出实际变更；`messages` 的 `before` ≤ 2048 字符、`limit` 1..100（默认 30）。会话与运行语义详见 [conversations 叶子](../conversations/README.md)。

#### R2.5 runs

| 方法 | 路径 | 处理函数 | 下游调用 |
| --- | --- | --- | --- |
| POST | `/api/threads/{thread_id}/runs` | `submit(...)` | `require_runtime` → `get_thread` → `catalog.get` → `RunRepository.submit(thread_id, text, client_request_id)`，202 |
| POST | `/api/runs/{run_id}/retry` | `retry(...)` | `require_runtime` → `RunRepository.retry(run_id, client_request_id)`，202 |
| GET | `/api/runs/{run_id}` | `snapshot(...)` | `RunRepository.snapshot(run_id)`（不要求 runtime 就绪） |
| GET | `/api/runs/{run_id}/events` | `events(...)` | `RunRepository.snapshot` + `RunRepository.events` + SSE `stream()`（R3） |

`submit` 请求体 `SubmitRun`：`text` 1..50000 字符且去空白非空，`client_request_id` 1..200 字符且非空白；`retry` 只需请求键。

#### R2.6 speech

| 方法 | 路径 | 处理函数 | 下游调用 |
| --- | --- | --- | --- |
| POST | `/api/messages/{message_id}/speech` | `synthesize(...)` | `require_runtime` → `SpeechRepository.enqueue(message_id, client_request_id)`，202 |
| GET | `/api/speech/{job_id}` | `snapshot(...)` | `SpeechRepository.get(job_id)` |
| GET | `/api/audio/resources/{resource_id}` | `resource(...)` | `SpeechRepository.resource(resource_id)` → `audio_path(...)` → `FileResponse(..., media_type="audio/wav", headers={"X-Content-Type-Options":"nosniff"})` |

音频读取由 Starlette `FileResponse` 处理 `Range` 请求（服务端不自行解析 Range），详见 [speech 叶子](../speech/README.md)。

#### R2.7 settings

| 方法 | 路径 | 处理函数 | 下游调用 |
| --- | --- | --- | --- |
| GET | `/api/settings` | `settings(request)` | `readiness()` → `asyncio.to_thread(read_settings, ...)`；设置 `read_only=False` 并计算 `capabilities.chat/speech` |
| GET | `/api/settings/models` | `models(request)` | `ModelSettingsService.get()` |
| PUT | `/api/settings/models` | `save_models(body, request)` | `ModelSettingsService.save(body)` |
| POST | `/api/settings/models/apply` | `apply_models(body, request)` | `ModelSettingsService.apply(body.expected_version)` |

详见 [settings 叶子](../settings/README.md)。

#### R2.8 service

| 方法 | 路径 | 处理函数 | 下游调用 |
| --- | --- | --- | --- |
| GET | `/api/service` | `status(request)` | 读取 `settings.owner_token`、`runtime.ready/error_code`、`runtime.memory.ready/error_code`，返回 `managed/startup_mode` |
| POST | `/api/service/shutdown` | `shutdown(request, x_mybot_owner)` | `secrets.compare_digest` 校验 owner token（无 token/头不符 → 403 `owner_required`）；无回调 → 409 `shutdown_unavailable`；否则调用 `app.state.shutdown_callback`，202 |

#### R2.9 legacy

| 方法 | 路径 | 处理函数 | 下游调用 |
| --- | --- | --- | --- |
| GET | `/api/legacy/threads` | `discover(...)` | `LegacyService.discover(cursor)` |
| POST | `/api/legacy/threads/{source_id}/import` | `import_thread(...)` | `LegacyService.request_import(source_id, character_id, title)`，202 |
| GET | `/api/legacy/threads/{source_id}` | `import_status(...)` | `LegacyService.status(source_id)` |
| GET | `/api/legacy/threads/{source_id}/messages` | `preview(...)` | `LegacyService.preview(source_id)` |
| POST | `/api/cli/resolve` | `resolve(body, request)` | `LegacyService.resolve(source_id, character_id, title, memory_retrieval_enabled, memory_storage_enabled)` |

详见 [legacy 叶子](../legacy/README.md)。

### R3. SSE 事件流（`routes/runs.py` 的 `stream()`）

- 定位与签名：[server/routes/runs.py](../../../server/routes/runs.py) 内 `async def stream()`，由 `StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})` 消费。
- 调用方与条件：客户端 `GET /api/runs/{run_id}/events`。

| 输入 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `after` | `int` | 查询参数 | 0..2^63-1，默认 0 | 起始事件序号（不含） |
| `Last-Event-ID` | `str \| None` | 请求头 | ≤ 30 字符；必须是 ASCII 数字 | 断线续传；`after = max(after, int(header))` |
| `run_id` | `UUID` | 路径 | 必须存在且线程未删除 | 事件范围 |

处理顺序：
1. 校验 `Last-Event-ID`：非 ASCII 数字或超 2^63-1 → `invalid_event_id`；`after` 超过 `snapshot["last_event_sequence"]` → `invalid_event_id`。
2. 循环 `while not await request.is_disconnected()`：`repo.events(run_id, sequence)` 取最多 100 条；逐条按 `RunEvent` 校验并输出 `id: {sequence}\nevent: {type}\ndata: {JSON}\n\n`（`ensure_ascii=False`、紧凑分隔符）。
3. 每轮重新 `snapshot(run_id)`：若状态已在 `TERMINAL` 且 `sequence >= last_event_sequence` → 结束（先取事件再读终态，关闭完成竞态）。
4. 有事件则 `idle=0` 继续；无事件则 `idle+=1`，每 40 次输出 `: keep-alive\n\n`，`await asyncio.sleep(0.25)`。

输出：SSE 帧序列，事件类型限定为 `run.started / phase / message.committed / state.updated / memory.retrieved / run.completed / run.failed`。断线不取消后台运行；订阅是只读轮询。

### R4. `readiness`

- 定位与签名：[server/services/health.py](../../../server/services/health.py) 的 `async def readiness(database, *, configured: bool) -> ReadyStatus`。
- 调用方与条件：`GET /ready` 与 `GET /settings`；请求级执行。
- 输入：`database`（`app.state.database`）、`configured`（`bool(settings.db_url)`）。
- 功能：`await database.ping()`，任何异常按未连接处理；返回 `ReadyStatus(status, database, schema_version, capabilities)`。
- 输出字段：`database` 为 `connected` / `unavailable`（已配置但 ping 失败）/ `not_configured`（未配置）；`schema_version` 仅连接时给出（`database.schema_version`，即最后一个迁移版本）；`capabilities.memories=connected`。`chat`/`speech` 由调用方（路由）补充为 `database=="connected" and runtime.ready and models.active is not None`。

### R5. `atomic_write`

- 定位与签名：[server/services/files.py](../../../server/services/files.py) 的 `def atomic_write(path: Path, content: str)`（同步）。
- 调用方：`CharacterCatalog.write_profile`（[characters 叶子](../characters/README.md)）与 `ModelSettingsService.save`（[settings 叶子](../settings/README.md)）。
- 功能：在目标同目录 `tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp")`；以 UTF-8、`newline=""`（保留原文换行，不做平台转换）写入并 `flush`+`os.fsync`；`os.replace` 原子替换；`finally` 中删除残留临时文件。
- 异常：磁盘/权限错误向上抛出，由调用方或全局处理器处理；不会留下半写文件。

## 请求与响应协议（`server/classes/`）

- 所有写请求继承 `RequestModel`（`ConfigDict(extra="forbid")`）：多余字段直接 422 `invalid_request`，不回显输入。
- `classes/runs.py` 关键 DTO：
  - `CreateThread`：`character_id` 1..255；`title` 默认 `"新对话"`（1..200）；两个记忆开关默认 `False`；`source` 为 `desktop|cli`（默认 `desktop`）。
  - `UpdateThread`：`expected_version >= 1`，可选 `title` / `memory_retrieval_enabled` / `memory_storage_enabled`。
  - `CheckpointSync`：`expected_version`、`dry_run`（默认 `False`）、`checkpoint_id`（≤ 200）。
  - `ImportThread`：`character_id`、`title` 默认 `"CLI 历史"`；`ResolveCLI` 在此基础上加 `source_id` 与两个记忆开关。
  - `SubmitRun`：`client_request_id` 非空白；`text` 非空白、≤ 50000（不做其他改写）。
  - `Message` / `RunSummary` / `RunSnapshot` / `Thread` / `ThreadPage` / `MessagePage` / `MemoryStatus` / `CheckpointSyncResult` / `RunEvent` / `SpeechJob`：响应模型；`RunSnapshot = RunSummary + messages + last_event_sequence`；`RunEvent.type` 与数据库 CHECK 一致；`SpeechJob.resource_url` 为 `/api/audio/resources/{resource_id}`。
- `classes/api.py`：`ServiceError(code, message, status_code=400, *, details=None)` 供全部服务抛出；`ErrorResponse/ErrorDetail` 描述错误信封；`HealthStatus`、`Capabilities`、`ReadyStatus`、`ModelStatus`、`SettingsStatus` 为只读响应。
- `classes/settings.py`：`PUBLIC_NODES = {main, participant_state, memory_query, memory_summary, chunking, tts}`；`ProfileWrite`（`expected_version` 固定 64 位、`text` ≤ 200000 且非空白）；`NodeModelSettings`（`provider` 三选一、`model`、`thinking`、`reasoning_effort`）；`ModelSettingsWrite.nodes` 的键必须 ⊆ `PUBLIC_NODES`；`ApplyModelSettings.expected_version` 固定 64 位。

## 分支与异常链

| 条件 | 处理 | 终点 |
| --- | --- | --- |
| Origin 不在白名单或多 Origin | 403 `origin_forbidden` | R1 提前返回，不进入路由 |
| 写方法缺少 `X-Mybot-Client: mybot-desktop` | 403 `client_header_required` | R1 提前返回 |
| 未知 Host | TrustedHost 拒绝（框架行为） | 未到路由 |
| 请求体多余字段 / 类型或范围不符 | 422 `invalid_request`，固定文案 | B5 |
| 未知路径 | 404 `not_found` | B5 |
| 服务层校验失败（如 `thread_busy`、`thread_conflict`） | 对应状态码与 code；`details` 附上下文 | B5 `service_error` |
| 连接池未就绪 | 503 `database_unavailable` | 路由 helper 抛 `ServiceError` |
| 运行执行器未就绪 | 503 `runtime_unavailable` | `require_runtime` |
| 模型配置缺失/无效 | 503 `model_config_unavailable` | `models.snapshot()`/`read_settings` 抛 `ServiceError` |
| 数据库连接异常 | 503 `database_unavailable` | B5 `database_error` |
| 其他未处理异常 | 500 `internal_error`，不暴露内部细节 | B5 `unexpected_error` |
| 未配置数据库 | `/ready` 503；记忆/会话接口 503；角色/图片/设置仍可用 | B3 降级规则 |
| 已配置数据库但启动失败 | 服务拒绝启动（进程退出） | B3 |

## 输入输出示例

提交运行（R2.5，成功）：

```http
POST /api/threads/6d0b8e18-.../runs
X-Mybot-Client: mybot-desktop
Content-Type: application/json

{"text": "晚上好。", "client_request_id": "req-0001"}
```

```json
{"run_id": "3f8c1c2e-...", "status": "queued"}
```

错误信封（B5）：

```json
{"error": {"code": "thread_busy", "message": "该会话仍有未完成运行。", "details": {"run_id": "3f8c1c2e-..."}}}
```

SSE 帧（R3，`event` 行取自 `RunEvent.type`）：

```text
id: 3
event: phase
data: {"run_id":"3f8c1c2e-...","sequence":3,"type":"phase","payload":{"phase":"replying"},"created_at":"2026-09-23T10:00:00Z"}

: keep-alive
```

## 关联文档与验证依据

- 上级：[服务总览](../README.md)；系统总览：[README/README.md](../../README.md)
- 同级叶子：[runtime](../runtime/README.md) · [conversations](../conversations/README.md) · [legacy](../legacy/README.md) · [memory](../memory/README.md) · [speech](../speech/README.md) · [settings](../settings/README.md) · [characters](../characters/README.md) · [persistence](../persistence/README.md)
- 协作模块：[config/README.md](../../config/README.md) · [core/README.md](../../core/README.md) · [agent/README.md](../../agent/README.md)
- 测试依据：[tests/server/test_api.py](../../../tests/server/test_api.py)（离线 API、访问规则、错误信封、DTO 校验）、[tests/server/controlled.py](../../../tests/server/controlled.py)（受控工厂）
- 迁移自原 `server/README.md` 的启动与配置说明（原文件已移除）：运行环境 Python 3.11+，安装 `python -m pip install -r server/requirements.txt`，运行 `python -m server`；固定监听 `127.0.0.1`，默认端口 8765；使用 Windows Selector 事件循环、单进程、不启用 reload；HTTP、对话/TTS 执行器与记忆执行器各自拥有事件循环与数据库连接池。依赖版本见 [constraints.txt](../../../server/constraints.txt)。
- 迁移自原文档的验证记录（未在本次文档编写中重跑）：2026-09-21 记录 48 项服务测试全部通过（20 项基础 API、3 项真实图/TTS 节点/状态投影、25 项独立 PostgreSQL 集成）；2026-09-22 记录 52 项服务测试通过（29 项独立 PostgreSQL 集成）。本轮仅静态核对源码，未执行测试。
