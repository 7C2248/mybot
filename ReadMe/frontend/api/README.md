# 服务适配与协议（api/）

## 职责与入口

本目录是前端与“本机 API 服务 / 演示数据”之间的唯一适配层：定义 wire 格式的 Zod DTO、HTTP/SSE 传输、演示适配器以及 DTO 到前端领域类型的映射。模块类型为“服务适配器 + 协议定义”，不是 Agent 图节点。

| 文件 | 类型 | 源码 |
| --- | --- | --- |
| HTTP/SSE 适配器 | `HttpService` 类、`ApiError`、`errorText`、`localBaseUrl`、`toMessage`、`toThread` | [http.ts](../../../frontend/src/shared/api/http.ts) |
| 协议定义 | 全部 Zod DTO（无执行逻辑） | [contracts.ts](../../../frontend/src/shared/api/contracts.ts) |
| 演示适配器 | `demoService`、`demoMemories`、`createDemoThreads` | [demo.ts](../../../frontend/src/shared/api/demo.ts) |

- **入口 1**：`WorkspaceStore` 在构造/切换模式时选择 `new HttpService(baseUrl)` 或 `demoService`，见 [app 叶子](../app/README.md) 的 B2/B3、R7。
- **入口 2**：各功能页通过 `workspaceStore.service` 直接调用 `HttpService` 方法（记忆页、会话管理、设置页、语音播放器），或通过 `workspaceStore` 间接调用（发送、历史、档案）。
- **上游**：`store.ts`、`features/*`；**下游**：本机 API 服务（[server/README.md](../../server/README.md)）与浏览器内演示数据。
- **触发时机**：`refresh()` 的 5 秒轮询、用户操作、SSE 流读取。

## 调用链总览

```text
B1 new HttpService(baseUrl)：localBaseUrl 规范化 → 保存 fetcher
B2 demoService 模块级对象：characters()/memories()/run() 三个方法
B3 contracts.ts 模块求值：导出 DTO，无副作用

R1 HttpService.json()：统一请求 → check() 错误解析 → schema.safeParse()
R2 端点方法（R2.1~R2.8）：健康/就绪、角色与档案、会话、运行与 SSE、记忆、设置、语音、检查点同步
R3 characters()：先列 id，再逐个 character(id) 并绝对化资源 URL
R4 events()：fetch 流式读取 → 分帧 → JSON 解析 → 序号去重 → emit
R5 resource()：白名单正则校验后拼接 baseUrl
R6 demoService.characters()/memories()/run()：读取本地 manifest、过滤示例记忆、模拟 phase 与回复
R7 toMessage()/toThread()：DTO → 前端领域类型
```

## 构建链

### B1. `new HttpService(baseUrl, fetcher)`

- 定位与签名：`class HttpService`，构造函数 `(baseUrl = 'http://127.0.0.1:8765', private fetcher: typeof fetch = (...args) => fetch(...args))`。
- 调用方与条件：`store.ts` 模块求值、`switchMode()`、设置页切换服务地址。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `baseUrl` | `string` | 默认 `http://127.0.0.1:8765` | 设置页“服务地址”或 `local_service_status.base_url` |
| `fetcher` | `typeof fetch` | 默认全局 `fetch` | 测试注入点 |

功能与内部调用：`localBaseUrl(input)` 用 `new URL` 解析并校验：协议必须 `http:`、主机名必须 `localhost` 或 `127.0.0.1`、不得含凭据/查询/哈希、路径必须为 `/`；随后把主机名统一改写为 `127.0.0.1` 并返回 `url.origin`。`this.mode` 固定为 `'service'`。

输出：可调用的适配器实例。异常与边界：非法地址抛 `Error('服务地址必须是本机 HTTP 地址，例如 http://127.0.0.1:8765。')`，由设置页展示。

### B2. `demoService` 模块级对象

- 定位与签名：`export const demoService: ChatService = { mode: 'demo', characters, memories, run }`；无构造参数。
- 调用方与条件：浏览器默认模式、设置页切到“演示模式”时被 `switchMode` 选中。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `/local-characters/manifest.json` | 静态资源 | 缺失时抛错 | 由 `npm run assets` 从 `../Character/<角色>/` 预生成 |
| `demoMemories` | `Memory[]` | 固定 3 条 SuLi 示例 | 演示记忆，含一条全空元数据 |
| `createDemoThreads()` | `() => Thread[]` | 固定 `demo-suli` | 初始演示会话，含 4 条虚构消息 |

功能：`characters()` 用 `characterSchema` 数组校验 manifest；`memories(characterId)` 过滤 `demoMemories`；`run(thread, requestId, emit)` 依次 `emit({ type: 'phase', phase: 'replying' })`、等待 650ms、`emit({ type: 'phase', phase: 'reviewing' })`、再等 650ms，按 `thread.messages` 中 user 消息数量取 `replies[round % 3]`，`emit({ type: 'message.committed', message: { id: crypto.randomUUID(), role: 'assistant', ... } })`。不访问数据库、模型或 TTS。

### B3. `contracts.ts` 协议定义

无函数、无副作用，仅导出 zod schema 与推断类型。生产方是本机 API，消费方是 `HttpService` 与 `WorkspaceStore`；完整 DTO 清单见下文 R2 与“协议目录”。

## 运行链

### R1. `HttpService.json()`（统一请求入口）

- 定位与签名：`private async json<T extends z.ZodType>(path, schema: T, method = 'GET', body?, signal?, allowUnavailable = false): Promise<z.infer<T>>`。
- 调用方与条件：R2 的全部端点方法；不对外导出。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `path` | `string` | 各端点方法 | 以 `/` 开头，自动补 `/api` 前缀 | 请求路径 |
| `schema` | `z.ZodType` | `contracts.ts` 或本地 schema | 必填 | 响应校验 |
| `method` | `'GET' \| 'POST' \| 'PUT' \| 'PATCH' \| 'DELETE'` | 端点方法 | 默认 `GET` | HTTP 方法 |
| `body` | `unknown` | 端点方法 | `undefined` 时不带 body 与 `Content-Type` | 请求体 |
| `signal` | `AbortSignal` | store 的 `lifecycle`/历史控制器 | 可选；与 15 秒超时 `AbortSignal.any` 合并 | 取消 |
| `allowUnavailable` | `boolean` | `ready()` 传 `true` | 默认 `false` | 允许 503 继续解析 |

功能与内部调用：
1. 固定超时 `AbortSignal.timeout(15_000)`。
2. `fetcher(baseUrl + '/api' + path, { method, headers: { 'X-Mybot-Client': 'mybot-desktop', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), body: JSON.stringify(body), signal })`。
3. `allowUnavailable && response.status === 503` 时跳过 `check()`，否则调用 `check(response)`。
4. `schema.safeParse(await response.json())`；失败抛 `ApiError('服务响应格式不兼容，请检查服务版本。', 'invalid_response')`。
5. `catch`：若 `signal?.aborted` 或已是 `ApiError` 原样抛出；否则包装为 `ApiError('无法连接本机服务或请求超时，请检查服务状态后重试。')`（`code = 'connection_failed'`、`status = 0`）。

`check(response)`：非 2xx 时解析 `{ error: { code, message, details } }`，成功则抛 `new ApiError(message, code, status, details ?? {})`，否则抛通用 `http_error`。

`ApiError` 字段与语义：

| 成员 | 类型 | 含义 |
| --- | --- | --- |
| `code` | `string` | 默认 `'connection_failed'`；服务端错误码或 `invalid_response`/`http_error` |
| `status` | `number` | HTTP 状态码；0 表示连接失败/超时 |
| `details` | `Record<string, unknown>` | 服务端 `error.details`，如 `thread_busy` 的 `run_id` |
| `uncertain` | `getter: boolean` | `status === 0 \|\| status >= 500`；发送恢复链据此保留 `pending` |

`errorText(error)`：`Error` 取 `message`，否则返回“请求失败，请重试。”。

### R2. 端点方法一览

所有路径均相对 `baseUrl + /api`；返回值为通过对应 schema 校验后的数据（多数方法在内部用 `toThread`/`toMessage` 转换）。

| 方法 | HTTP | 路径 | 响应 schema | 用途与消费方 |
| --- | --- | --- | --- | --- |
| `health(signal?)` | GET | `/health` | `{ service: 'mybot', status: 'ok' }` | 探活；`refresh()` |
| `ready(signal?)` | GET | `/ready` | `readinessDto`（允许 503） | 数据库与能力；`refresh()`、设置页 |
| `settings()` | GET | `/settings` | `settingsDto` | 脱敏配置与模型节点状态；设置页 |
| `characters(signal?)` | GET | `/characters` | `{ id }[]` 再逐个详情 | 角色列表；`refresh()`（见 R3） |
| `character(id, signal?)` | GET | `/characters/{id}` | `characterSchema` | 单个档案与素材；`characters()`、`reloadCharacter` |
| `saveProfile(id, language, text, version)` | PUT | `/characters/{id}/profiles/{language}` | `characterSchema` | 档案写回，body `{ expected_version, text }`；角色页 |
| `threads(cursor?, signal?, deleted = false)` | GET | `/threads?deleted=&cursor=` | `pageOf(threadDto)` | 会话分页；`loadThreads`、会话管理 |
| `createThread(characterId, title, policy)` | POST | `/threads` | `threadDto` | 新建会话，body 含两项记忆开关；新建对话框 |
| `thread(id)` | GET | `/threads/{id}` | `threadDto` | 单会话版本/策略；轮询、编辑器重读 |
| `updateThread(id, version, title, policy)` | PATCH | `/threads/{id}` | `threadDto` | 保存标题与记忆策略；会话编辑器 |
| `deleteThread(id, version)` | DELETE | `/threads/{id}` | `threadDto` | 移入回收站 |
| `restoreThread(id, version)` | POST | `/threads/{id}/restore` | `threadDto` | 回收站恢复 |
| `purgeThread(id, version)` | DELETE | `/threads/{id}/purge` | `{ deleted: boolean }` | 永久删除 |
| `legacyThreads(cursor?)` | GET | `/legacy/threads?cursor=` | `pageOf(legacyDto)` | 旧 CLI 历史列表；`refresh()`、会话管理 |
| `importLegacy(source, character, title)` | POST | `/legacy/threads/{source}/import` | `legacyDto` | 关联并导入旧历史 |
| `legacyPreview(source)` | GET | `/legacy/threads/{source}/messages` | `legacyPreviewDto` | 导入前预览 |
| `messages(id, before?, signal?)` | GET | `/threads/{id}/messages?before=` | `pageOf(messageDto)` | 历史分页；`loadHistory`、消息列表 |
| `state(id, signal?)` | GET | `/threads/{id}/state` | `stateDto` | 世界/角色/用户状态；`loadHistory`、`watch` |
| `memoryStatus(id, signal?)` | GET | `/threads/{id}/memory-status` | `memoryStatusDto` | 后台记忆任务状态；`loadMemoryStatus` |
| `submit(id, text, key, signal?)` | POST | `/threads/{id}/runs` | `{ run_id, status }` | 提交输入，body `{ text, client_request_id }`；`submitPending` |
| `retry(id, key, signal?)` | POST | `/runs/{id}/retry` | `{ run_id, status }` | 重试失败/中断运行，body `{ client_request_id }` |
| `snapshot(id, signal?)` | GET | `/runs/{id}` | `runSnapshotDto` | 运行快照（含消息与最后事件序号）；`watch` |
| `memories(character, query = '', cursor?, signal?)` | GET | `/characters/{id}/memories?query=&cursor=` | `pageOf(memoryDto)` | 记忆原文分页；记忆页 |
| `memory(character, id, signal?)` | GET | `/characters/{id}/memories/{id}` | `memoryDto` | 单条原文；记忆详情 |
| `models()` | GET | `/settings/models` | `modelSettingsDto` | 模型设置读取；设置页 |
| `saveModels(version, nodes)` | PUT | `/settings/models` | `modelSettingsDto` | 保存模型配置，body `{ expected_version, nodes }` |
| `applyModels(version)` | POST | `/settings/models/apply` | `modelSettingsDto` | 应用已保存配置，body `{ expected_version }` |
| `speech(messageId, key)` | POST | `/messages/{id}/speech` | `speechDto` | 提交语音任务，body `{ client_request_id }` |
| `speechJob(id, signal?)` | GET | `/speech/{id}` | `speechDto` | 查询语音任务状态 |
| `syncCheckpoint(id, expectedVersion, dryRun, checkpointId?)` | POST | `/threads/{id}/checkpoint-sync` | `checkpointSyncDto` | 检查点差异预览/确认，body 含 `dry_run` 与可选 `checkpoint_id` |
| `events(id, after, signal, emit)` | GET(SSE) | `/runs/{id}/events?after=` | `eventDto` 逐帧 | 运行事件流（见 R4） |

### R3. `characters()` 聚合

- 定位与签名：`async characters(signal?: AbortSignal): Promise<Character[]>`。
- 功能与内部调用：先 `json('/characters', z.array(z.object({ id: z.string() })))`，再 `Promise.all(list.map(({ id }) => this.character(id, signal)))`。
- `character(id)` 在 schema 校验后把 `assets[].url` 经 `resource()`（R5）绝对化，返回 `{ ...value, assets: [...绝对 URL] }`。
- 输出：完整 `Character[]`。异常：任一详情失败则整体失败，由调用方决定降级。

### R4. `events()`（SSE）

- 定位与签名：`async events(id: string, after: number, signal: AbortSignal, emit: (event: ServiceEvent) => void): Promise<void>`。
- 调用方与条件：`WorkspaceStore.watch()` 的循环体；运行未终态时反复调用以续传。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `id` | `string` | `runId` | 必填 | 事件所属运行 |
| `after` | `number` | 本地缓存 `after` | 必填 | 只接收序号更大的事件 |
| `signal` | `AbortSignal` | 监控控制器 + 生命周期 | 必填 | 取消 |
| `emit` | `(event) => void` | `watch` 闭包 | 必填 | 交给 `applyEvent` |

功能与内部调用：
1. 新建内部 `controller`，`combined = AbortSignal.any([signal, controller.signal])`；`timer = setTimeout(() => controller.abort(), 30_000)` 作为“无活动超时”，每读到数据块就重置。
2. `fetcher(baseUrl + '/api/runs/{id}/events?after={after}', { signal: combined, headers: { Accept: 'text/event-stream' } })`；`check(response)` 后要求 `response.body` 存在且 `content-type` 含 `text/event-stream`，否则抛 `invalid_response`。
3. 用 `TextDecoder` 流式解码；按 `/\r?\n\r?\n/` 分帧；每帧把以 `data:` 开头的行去掉前缀（含可选一个空格）后用 `\n` 连接；空帧跳过。
4. `eventDto.parse(JSON.parse(data))`；`event.run_id !== id` 抛“事件所属运行不一致。”；仅当 `event.sequence > after` 时 `emit(event)` 并推进局部 `after`。
5. 缓冲超过 2 MB 抛“事件数据过大。”；`finally` 清理定时器、中止内部控制器、`reader.cancel()` 与 `releaseLock()`。

输出：无返回值；事件通过回调消费。副作用：读取流；取消时中止请求。

### R5. `resource(path)`

- 定位与签名：`resource(path: string): string`。
- 输入：服务端返回的资源路径，如 `/api/resources/SuLi-1` 或 `/api/audio/resources/xxx`。
- 校验：必须匹配 `/^\/api\/(?:resources|audio\/resources)\/[a-zA-Z0-9-]+$/`，否则抛 `ApiError('服务返回的资源地址无效。', 'invalid_response')`。
- 输出：`this.baseUrl + path`。消费方：`character()` 的图片、`SpeechPlayer` 的音频。

### R6. `demoService` 方法

| 方法 | 签名 | 输入与前置条件 | 输出与副作用 |
| --- | --- | --- | --- |
| `characters` | `() => Promise<Character[]>` | 需要 `public/local-characters/manifest.json` | `z.array(characterSchema).parse(...)`；`!response.ok` 抛“角色资源未准备好，请运行 npm run assets 后刷新。” |
| `memories` | `(characterId) => Promise<Memory[]>` | 必填角色 id | 过滤 `demoMemories`；无网络 |
| `run` | `(thread, _requestId, emit) => Promise<void>` | 由 `WorkspaceStore.send` 演示分支调用 | 两次 phase + 一次 `message.committed`；`requestId` 未使用 |

### R7. `toMessage()` / `toThread()`

| 函数 | 输入 | 输出 | 字段映射 |
| --- | --- | --- | --- |
| `toMessage` | `messageDto` | `Message` | `created_at → createdAt`、`run_id → runId`、`timestamp_estimated → timestampEstimated`；其余同名字段 |
| `toThread` | `threadDto` | `Thread` | `current_run ?? latest_run → run`；`memory_retrieval_enabled/storage_enabled → memoryPolicy`；`deleted_at → deletedAt`、`history_notice → historyNotice`；`messages: []`、`draft: ''`、`unread: false`、`phase: 'idle'` 为占位初始值 |

`toThread` 不填充消息正文，正文由 `messages()` 或快照单独获取。

## 分支与异常链

1. **连接失败/超时**：`json()` 捕获非 `ApiError` 异常并包装 `connection_failed`、`status = 0`，`uncertain` 为真；发送恢复链据此保留 `pending`。
2. **服务返回 503（未就绪）**：仅 `ready()` 传 `allowUnavailable = true`，仍解析 `readinessDto`；其余端点按错误处理。
3. **响应格式不兼容**：`safeParse` 失败抛 `invalid_response`；`events()` 的 content-type/run_id/缓冲上限同样走该错误码。
4. **SSE 序号回退或重复**：`event.sequence <= after` 的帧被忽略，保证 `applyEvent` 幂等。
5. **SSE 30 秒无数据**：内部 `controller.abort()`，`watch()` 捕获后按退避重试，界面显示“同步暂时中断，正在恢复”。
6. **非法资源路径**：`resource()` 抛错；角色图片与语音播放器各自捕获，语音显示“音频资源读取失败，可重新生成。”。
7. **演示资源缺失**：`demoService.characters()` 抛出带操作指引的错误，`WorkspaceStore` 写入 `resourceError` 并显示全局提示。
8. **地址非法**：`localBaseUrl` 抛错，设置页展示且不切换模式。

## 输入输出示例

**示例 1（R1/R2，提交运行）**

```json
// POST /api/threads/t-1/runs
{ "text": "雨停了吗？", "client_request_id": "b1f6…" }
// 200
{ "run_id": "run-9", "status": "queued" }
```

**示例 2（R4，SSE 帧）**

```text
data: {"run_id":"run-9","sequence":3,"type":"phase","payload":{"phase":"replying"},"created_at":"2026-09-23T10:00:00Z"}

data: {"run_id":"run-9","sequence":4,"type":"message.committed","payload":{"message":{"id":"m-2","thread_id":"t-1","run_id":"run-9","sequence":2,"role":"assistant","text":"嗯，已经停了。","created_at":"2026-09-23T10:00:02Z"}},"created_at":"2026-09-23T10:00:02Z"}
```

`after = 2` 时两帧都会 emit；`after = 4` 时两帧都被忽略。

**示例 3（R2，错误体）**

```json
// 409
{ "error": { "code": "version_conflict", "message": "会话已被其他客户端修改。", "details": { "expected_version": 3, "actual_version": 4 } } }
```

`check()` 抛出 `ApiError`，`code = "version_conflict"`、`status = 409`、`details` 原样携带；会话编辑器据此提示重新预览。

## 协议目录（contracts.ts）

| DTO | 关键字段 | 使用方 |
| --- | --- | --- |
| `memoryStatusDto` | `status ∈ {disabled, idle, pending, running, failed, completed}`、`job_id` | `loadMemoryStatus`、`RunStatus` |
| `messageDto` | `id/thread_id/run_id/source/sequence/role/text/created_at`、`timestamp_estimated` | 历史、快照、事件、检查点预览 |
| `runDto` / `runSnapshotDto` | `status ∈ {queued, running, completed, completed_with_warnings, failed, interrupted}`、`phase`、`retry_of`、`warnings`、快照追加 `messages` 与 `last_event_sequence` | `watch`、`send` |
| `terminal(run)` | 辅助函数：`status` 不属于 `queued/running` 即为终态 | `store.ts` |
| `threadDto` | `current_run/latest_run`、`version`、`memory_policy_version`、两项记忆开关、`source ∈ {desktop, cli, legacy}`、`history_notice` | 会话列表与设置 |
| `legacyDto` / `legacyPreviewDto` | 旧 CLI 来源 id、关联状态、预览消息与 `notice` | 会话管理 |
| `pageOf(schema)` | 泛型分页 `{ items, next_cursor }` | 会话、历史、记忆 |
| `memoryDto` / `hitDto` | `memory/importance/event_date/update_time/keywords`；`hitDto` 为检索命中的精简字段 | 记忆页、情境面板 |
| `stateDto` | `world_state`、`character_state`、`user_state`（`location/mood/body/clothing/hearing`）、`retrieved_memories` | 情境面板、事件合并 |
| `eventDto` | `run_id`、`sequence`（正整数）、`type ∈ {run.started, phase, message.committed, state.updated, memory.retrieved, run.completed, run.failed}`、`payload` | SSE |
| `capabilitiesDto` / `readinessDto` | 7 项能力布尔、`status`、`database ∈ {connected, not_configured, unavailable}` | 连接页、`Composer` 可用性 |
| `settingsDto` | `model_configuration`、`database_configured`、`capabilities`、`models` 概览 | 设置页 |
| `nodeModelDto` / `modelSettingsDto` | `provider ∈ {deepseek, moonshot, llama_cpp}`、`model`、`thinking`、`reasoning_effort`；设置含 `saved_version/active_version/pending_changes/nodes/effective_from/runtime_busy` | 模型设置 |
| `speechDto` | `status ∈ {queued, running, completed, failed, interrupted}`、`resource_url` | 语音播放器 |
| `checkpointSyncDto` | `dry_run`、`checkpoint_id`、`delete_count`、`delete_preview`、`preview_truncated`、`run_count`、`version`、`state` | 检查点同步 |

## 关联文档与验证依据

- 上级：[frontend/README.md](../README.md)
- 同级：[app/README.md](../app/README.md)（调用方）· [chat/README.md](../chat/README.md)（历史/语音）· [memory/README.md](../memory/README.md) · [conversations/README.md](../conversations/README.md) · [settings/README.md](../settings/README.md) · [platform/README.md](../platform/README.md)
- 服务端权威契约：[server/README.md](../../server/README.md) · 服务接口清单：[frontend/API_CONTRACT.md](../../../frontend/API_CONTRACT.md)
- 单元测试：[http.test.ts](../../../frontend/src/shared/api/http.test.ts)（地址与资源白名单、客户端头与错误码、SSE 分片/注释/重复序号、检查点预览与确认）
- 端到端测试：[service.spec.ts](../../../frontend/e2e/service.spec.ts)（丢失回执恢复、SSE 去重、历史分页锚点、记忆分页）
- 历史验收记录（原 `frontend/README.md`，已迁移）：2026-09-21 覆盖发送响应丢失后的请求去重与音频资源加载；2026-09-22 覆盖发送回执与服务排队提示分开、后台记忆整理不阻塞下一轮回复、记忆状态读取失败不影响发送。均使用受控测试实现。
- 未验证项：`events()` 的 2 MB 缓冲上限与 30 秒超时仅有源码依据，本次未构造超限流；服务端行为以 [server/README.md](../../server/README.md) 为准。
