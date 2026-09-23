# 应用外壳与状态中心（app/）

## 职责与入口

本目录承载前端的两项全局能力：React 挂载与错误边界，以及唯一的 `WorkspaceStore` 状态中心。它不是 Agent 图节点，也不是服务端节点，模块类型为“应用外壳 + 全局状态类”。

| 文件 | 类型 | 源码 |
| --- | --- | --- |
| 前端入口 | `ErrorBoundary` 类组件 + `createRoot` 挂载 | [main.tsx](../../../frontend/src/main.tsx) |
| 应用外壳 | `App` 函数组件：一级导航、标题栏、侧栏、全局提示、新建对话、简化模式切换 | [App.tsx](../../../frontend/src/app/App.tsx) |
| 状态中心 | `WorkspaceStore` 类 + 单例 `workspaceStore` + `useWorkspace` | [store.ts](../../../frontend/src/app/store.ts) |

- **入口 1**：[index.html](../../../frontend/index.html) 通过 `<script type="module" src="/src/main.tsx">` 加载前端入口，模块顶层调用 `createRoot(document.getElementById('root')!).render(...)`。
- **入口 2**：`store.ts` 模块顶层创建单例 `workspaceStore` 并导出 `initializeWorkspace()`；`App` 在挂载 `useEffect` 中调用。
- **上游**：`index.html`；**下游**：[http.ts](../api/README.md)、[demo.ts](../api/README.md)、[platform/window.ts](../platform/README.md)、[platform/service.ts](../platform/README.md) 与全部 `features/*` 页面。
- **触发时机**：页面加载即执行；服务模式每 5 秒轮询一次 `refresh()`；简化模式切换由标题栏按钮触发。

## 调用链总览

区分构建、运行、事件与用户交互关系，编号稳定，后文按编号展开：

```text
B1 main.tsx 模块求值：ErrorBoundary → createRoot().render(StrictMode → ErrorBoundary → App)
B2 store.ts 模块求值：读取 CONNECTION_KEY → new WorkspaceStore(selected, storage)
   → 导出 workspaceStore / initializeWorkspace / useWorkspace
   → window beforeunload 注册 dispose
B3 WorkspaceStore 构造：restore() → 按服务模式读取本地缓存 → 建立初始 Workspace

R1 App 挂载 useEffect → initializeWorkspace()
R2 WorkspaceStore.initialize()：演示模式只取 characters()；服务模式 refresh() + 5 秒定时器
R3 WorkspaceStore.refresh()：health/ready/characters → loadThreads → thread → loadHistory
   → loadMemoryStatus → legacyThreads/importLegacy → submitPending / watch
R4 用户导航 navigate(page) → 渲染对应 feature 页面（组件链见各自叶子文档）
R5 会话与发送：selectThread / createThread / loadThreads / send / submitPending / recover
R6 运行监控：watch() 循环 snapshot + events(SSE) → applySnapshot / applyEvent
R7 模式切换：switchMode(mode, baseUrl) → dispose + restore + initialize
R8 简化模式：App.toggleCompact(value) → enterCompact / leaveCompact（platform 叶子）
R9 订阅：useWorkspace() → useSyncExternalStore(workspaceStore.subscribe, workspaceStore.getSnapshot)
```

`R4` 之后进入各功能页自己的组件链；本页只说明外壳如何渲染它们。

## 构建链

### B1. `main.tsx` 模块求值

- 定位与签名：`frontend/src/main.tsx`，同步模块求值；`ErrorBoundary` 是继承 `Component` 的类。
- 调用方与条件：`index.html` 的 module script 执行时触发一次。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `#root` DOM 节点 | `HTMLElement` | 必填（`!` 断言） | `index.html` 的 `<div id="root">` |
| `./shared/theme.css` | CSS 副作用导入 | 必填 | 主题 token 与全部布局样式 |

功能与内部调用：
1. 定义 `ErrorBoundary`：`state = { failed: false }`，`getDerivedStateFromError()` 返回 `{ failed: true }`，`componentDidCatch(error, info)` 打印 `console.error('UI error', error, info.componentStack)`。
2. `render()` 在 `failed` 时输出 `div.empty`：标题“界面暂时无法显示”、说明“已保存的会话仍保留在当前设备。”和“重新加载”按钮（`location.reload()`）；否则原样渲染 `children`。
3. `createRoot(...).render(<StrictMode><ErrorBoundary><App /></ErrorBoundary></StrictMode>)`。

输出：已挂载的 React 树。副作用：注册全局错误捕获。异常与边界：React 渲染期异常被边界捕获并降级到静态提示，不自动重试。

### B2. `store.ts` 模块求值（连接选择与单例）

- 定位与签名：`frontend/src/app/store.ts` 顶层，同步；关键常量 `STORAGE_KEY`、`CONNECTION_KEY`、`serviceStorageKey(base)`。
- 调用方与条件：任何导入 `store.ts` 的模块首次加载时执行一次。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `window.localStorage` | `Storage \| undefined` | 可能不可用 | `browserStorage()` 捕获异常后返回 `undefined` |
| `CONNECTION_KEY` 内容 | JSON `{ mode, baseUrl }` | 缺省时按平台默认 | `mybot.frontend.connection.v1`，zod 校验 `mode ∈ {service, demo}` |
| `desktop` | `boolean` | 由 `isTauri()` 决定 | 平台探测，见 [platform 叶子](../platform/README.md) |

功能与内部调用：
1. `storage = typeof window !== 'undefined' ? browserStorage() : undefined`。
2. `selected = desktop ? new HttpService() : demoService`（默认桌面走真实服务，浏览器走演示）。
3. 尝试解析 `CONNECTION_KEY`：成功则 `selected = config.mode === 'service' ? new HttpService(config.baseUrl) : demoService`，`configured = true`；失败保持平台默认，绝不把服务数据当演示数据处理。
4. `export const workspaceStore = new WorkspaceStore(selected, storage)`。
5. 定义 `initializeWorkspace()`：`desktop && !configured` 时先 `getLocalServiceStatus()` 并 `switchMode('service', status.base_url)`，否则 `initialize()`。
6. `window.addEventListener('beforeunload', () => workspaceStore.dispose())`；导出 `useWorkspace()`。

输出：单例 `workspaceStore`、`initializeWorkspace`、`useWorkspace`。副作用：注册卸载清理。

### B3. `WorkspaceStore` 构造与 `restore()`

- 定位与签名：`new WorkspaceStore(service: ChatService | HttpService, storage?: Pick<Storage, 'getItem' | 'setItem'>)`；构造函数调用私有 `restore()`。
- 调用方与条件：B2 模块求值、`switchMode()` 切换服务后。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `service` | `ChatService \| HttpService` | 必填 | 演示适配器或 HTTP 适配器 |
| `storage` | `Pick<Storage,...>` | 可为 `undefined` | 本地缓存；缺失时 `storageWarning` 提示刷新丢失 |
| 持久化键 | `string` | 由 `get key()` 计算 | 演示模式用 `STORAGE_KEY`；服务模式用 `serviceStorageKey(baseUrl)` 按地址隔离 |

功能与内部调用：
1. 置 `storageWarning`；清空 `cache`/`reading`/`legacyScanCursor`。
2. 读取缓存：演示模式走 `restoreWorkspace(raw)`（`persistedSchema` 校验；`isBusy` 的线程改写为 `phase: 'error'` 并附“上次预览在回复完成前关闭”）；服务模式走 `cacheSchema` 校验，恢复 `activeThreadId`、`preferences`、`reading`、`speech`，线程列表初始为空。
3. 解析失败时把当前键加入 `blockedKeys`，保留原数据不再写入，并把 `storageWarning` 设为“本地缓存无法读取…”。
4. 组装 `this.value`：`{ ...data, mode, baseUrl, connection: 'connecting', connectionError: '', characters: [], loading: true, resourceError: '', storageWarning, speech, legacy: [], legacyError: '' }`。

输出：`getSnapshot()` 返回的初始 `Workspace`。副作用：无网络请求；服务模式线程正文不写入本地缓存。

## 运行链

### R1. `initializeWorkspace()`

- 定位与签名：`frontend/src/app/store.ts` 导出的异步函数；由 `App` 的挂载 `useEffect`（`[]` 依赖）调用。
- 调用方与条件：每次页面挂载；`StrictMode` 下可能重复调用，但 `initialize()` 有 `initialized` 守卫。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `desktop` | `boolean` | platform 探测 | 浏览器为 `false` | 决定是否自动探测本机服务 |
| `configured` | `boolean` | B2 的 `CONNECTION_KEY` 解析结果 | 无缓存时为 `false` | 已有连接偏好时不覆盖 |
| `getLocalServiceStatus()` | `Promise<LocalServiceStatus>` | Tauri IPC | 仅桌面调用 | 取得 `base_url` 后 `switchMode('service', base_url)` |

功能与内部调用：桌面且无连接偏好时调用 `getLocalServiceStatus()`（见 [platform 叶子](../platform/README.md)），成功则切换到服务模式并返回；否则 `await workspaceStore.initialize()`。失败被 `catch` 吞掉，保留 HTTP 状态与重试入口。

输出：无返回值。后续去向：进入 R2。

### R2. `WorkspaceStore.initialize()`

- 定位与签名：`WorkspaceStore.initialize(): Promise<void>`，`initialized` 标志保证只执行一次。
- 调用方与条件：R1 或 `switchMode()` 尾部。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `service.mode` | `'demo' \| 'service'` | 当前适配器 | 必填 | 分支选择 |
| `epoch` | `number` | 实例字段 | 每次 `dispose()` 自增 | 丢弃过期异步结果 |

功能与内部调用：
1. 记录 `epoch = this.epoch`。
2. 演示模式：`await this.service.characters()`，成功 `publish({ characters, loading: false, connection: 'connected' }, false)`；失败 `publish({ loading: false, resourceError, connection: 'disconnected' }, false)`，不建立轮询。
3. 服务模式：`await this.refresh()`，随后 `setInterval(() => void this.refresh(), 5000)` 存入 `this.timer`。

输出：无返回；通过 `publish` 更新快照。副作用：注册 5 秒轮询定时器。

### R3. `WorkspaceStore.refresh()`

- 定位与签名：`WorkspaceStore.refresh(): Promise<void>`；`refreshing` 标志防重入；`lifecycle.signal` 支持整体取消。
- 调用方与条件：R2、5 秒定时器、界面“重新连接”按钮、`manageThread`/`switchMode` 尾部。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `service.mode` | `'demo' \| 'service'` | 适配器 | 非服务模式直接返回 | 只轮询服务 |
| `epoch` / `signal` | `number` / `AbortSignal` | 实例 / 生命周期 | 每次异步返回后校验 | 防止过期写入 |

功能与内部调用（按顺序）：
1. `api.health(signal)` 探活；随后并行 `api.ready(signal)` 与 `api.characters(signal)`。
2. `publish({ ready, characters, connection: 'connected', connectionError: '', resourceError: '', loading: false }, false)`。
3. `ready.database === 'connected'` 时：
   - `loadThreads(false)`（R5）；
   - 取 `activeThreadId` 对应线程，`api.thread(id)` 后 `mergeRemoteThread`；若 `run` 变化或 `!historyLoaded` 则 `loadHistory(id)`；再 `loadMemoryStatus(id)`；
   - `api.legacyThreads()` 写入 `legacy`，并按 `legacyScanCursor` 继续翻页；对 `status === 'unlinked' && character_id` 的条目自动 `api.importLegacy(source_id, character_id, title ?? 'CLI 历史')`；
   - 遍历线程：有 `pending` 的 `submitPending(id)`，否则 `!terminal(run) || !historyLoaded` 时 `watch(id, run.id)`。
4. 线程详情 404 时 `forgetThread(id)`；其余异常由外层 `catch` 统一置 `connection: 'disconnected'`、`connectionError`、`loading: false`。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `connection` / `connectionError` | `'connected' \| 'disconnected'` / `string` | 请求成功或异常 | 覆盖写 | 标题栏、设置页、`Composer` 可用性 |
| `ready` | `Readiness` | `ready()` 成功 | 覆盖写 | 数据库/能力展示 |
| `characters` | `Character[]` | `characters()` 成功 | 覆盖写 | 各页面 |
| `legacy` / `legacyError` | `LegacyThread[]` / `string` | 列表读取成功/失败 | 覆盖写 | 会话管理、全局提示 |

副作用：自动导入旧 CLI 会话、触发运行监控；`refresh` 本身不写本地缓存（`publish` 默认 `persist=false`）。

### R4. `App` 渲染与用户导航

- 定位与签名：`App()` 函数组件；内部 `navigate(next: Page)`、`startNew(id)`、`toggleCompact(value)`。
- 调用方与条件：React 根渲染；`useWorkspace()` 订阅状态变化后重渲染。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `workspace.threads/activeThreadId/characters/preferences` | 快照字段 | `useWorkspace()` | 初始为空数组/默认偏好 | 计算当前线程与立绘 |
| `page` | `Page = 'chat' \| 'characters' \| 'memory' \| 'settings' \| 'conversations'` | `useState('chat')` | 默认对话页 | 一级导航 |
| `focus` / `compact` / `switching` | `boolean` | `useState(false)` | false | 专注阅读、简化模式、切换防重入 |
| `sidebarOpen` / `drawerOpen` / `narrow` | `boolean` | `useState`；`narrow` 由 `matchMedia('(max-width: 1179px)')` 更新 | false；`narrow` 初始 `window.innerWidth < 1180` | 会话导航抽屉与情境面板抽屉 |
| `newCharacter` / `title` / `memoryPolicy` / `creating` / `createError` | 新建对话表单 | `useState` | 角色默认 `thread?.characterId ?? characters[0]?.id ?? ''` | 新建会话对话框 |
| `memoryId` / `managedId` / `managedTab` | 深链参数 | `useState` | `managedTab` 默认 `'active'` | 打开记忆详情、会话管理定位 |
| `platformError` | `string` | `useState` | 空 | 窗口操作失败提示 |

功能与内部调用：
1. 三个 `useEffect` 同步 `document.documentElement.dataset`：`theme/accent/density`（来自偏好）与 `compact`（`String(compact)`）；`matchMedia` 监听 1179px 断点并在变化时关闭抽屉。
2. `inspectorVisible = narrow ? drawerOpen : preferences.inspector`。
3. `navigate(next)`：`setPage(next)`、退出专注、关闭侧栏；进入记忆页时清空 `memoryId`。
4. `toggleCompact(value)`：`switching` 防重入；进入时 `await enterCompact(preferences)` 后 `setCompact(true)`；退出时先 `flushSync(() => { setCompact(false); setPage('chat'); })` 再 `await leaveCompact()`，避免工作台尺寸覆盖浮窗尺寸；异常交给 `reportError`。
5. 渲染顺序：全局提示条（`storageWarning`/`resourceError`/`platformError`）→ 简化模式（`compact && thread` 时渲染 `CompactChat`）→ 否则 `app-shell`：`titlebar`（`desktop ? '桌面版' : '浏览器'` 与模式标签）→ `sidebar`（5 项导航、最近会话、加载更多、底部连接说明）→ `main-content`（服务未就绪/CLI 历史提示、工具栏、页面内容）。
6. 工具栏按钮：专注阅读切换 `focus`；简化模式调用 `toggleCompact(true)`；情境面板在窄屏切换 `drawerOpen`，宽屏写 `setPreferences({ inspector })`。
7. 新建对话 `Dialog`：角色 `<select>`、标题（`maxLength=80`）、服务模式下的 `MemoryPolicyFields`、提交调用 `workspaceStore.createThread(newCharacter, title, memoryPolicy)`，成功后关闭并 `navigate('chat')`。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `page` | `Page` | 点击导航 | 覆盖写 | 主内容分支 |
| `compact` | `boolean` | 简化模式切换成功/失败 | 覆盖写 | 顶层条件渲染 |
| `newCharacter` | `string \| null` | 新建/取消/成功 | 覆盖写 | 对话框显隐 |

副作用：写 DOM dataset、调用窗口 IPC、创建线程（服务端写入）。异常与边界：窗口操作异常只提示不崩溃；`createThread` 失败保留对话框并显示错误。

### R5. 会话与发送相关方法

以下方法同属运行链的不同触发点，签名与输入如下：

| 方法 | 签名 | 输入与前置条件 | 行为与输出 | 异常与边界 |
| --- | --- | --- | --- | --- |
| `selectThread` | `(id: string) => void` | 会话列表点击 | `publish({ activeThreadId })`、`markRead`；服务模式 `loadHistory(id)` | 不存在的 id 仍会切换（列表来源可信） |
| `loadThreads` | `(more = true) => Promise<void>` | `more` 为 false 表示首页刷新；`threadsCursor` 存在才继续翻页 | `api.threads(cursor)` → `mergeRemoteThread` → 合并/追加 → `publish`；若活动会话不在当前页且仍有游标，自动继续翻页找回 | `threadsLoading` 防重入；返回后校验 `epoch` |
| `createThread` | `(characterId, title, policy?) => Promise<void>` | 新建对话框提交 | 服务模式 `api.createThread`，演示模式本地构造；`publish` 到列表头并设为活动 | 服务端异常向上抛给对话框 |
| `send` | `(id, retry = false) => Promise<void>` | 草稿非空（非重试）或 `phase === 'error'`（重试） | 服务模式写入 `pending`（`crypto.randomUUID()` 请求键）后 `submitPending`；演示模式乐观追加用户消息并消费 `service.run` 事件 | 见下方 `submitPending` |
| `submitPending` | `(id) => Promise<void>`（私有） | 线程存在 `pending` 且不在 `running` 集合 | `retryOf ? api.retry(...) : api.submit(...)` → `api.snapshot` → 清 `pending` → `applySnapshot` → `watch` | `ApiError && !uncertain`：清 `pending`；重试保留草稿；普通发送在输入框已有新草稿时把被拒原文放入 `unsent`，否则回填到输入框；`thread_busy` 时用 `details.run_id` 接管监控；`uncertain`（status 0 或 ≥500）保留 `pending` 等待幂等恢复 |
| `recover` | `(id) => Promise<void>` | “恢复同步”按钮 | 有 `pending` 走 `submitPending`；否则 `watch` + `loadHistory` | 线程不存在直接返回 |
| `manageThread` | `(id, version, action) => Promise<void>` | 会话管理删除/恢复/永久删除 | `api.deleteThread/restoreThread/purgeThread`；非恢复动作 `forgetThread(id)`；随后 `refresh()` | 演示模式直接返回 |
| `updateConversation` | `(id, version, title, policy) => Promise<Thread \| undefined>` | 会话编辑器保存 | `api.updateThread` 后 `updateThread` 合并 `title/version/memoryPolicy` | 返回服务端线程供编辑器更新版本 |
| `setDraft` | `(id, draft) => void` | 输入框 `onChange` | `updateThread` 覆盖 `draft` | 无 |
| `restoreUnsent` | `(id) => void` | “有未发送的文字”恢复 | 仅当草稿为空时把 `unsent` 移回 `draft` | 草稿非空时按钮禁用 |
| `setPreferences` | `(patch) => void` | 设置页 | `preferencesSchema.parse({ ...旧值, ...patch })` 后 `publish` | 非法值由 zod 拒绝 |
| `markRead` | `(id) => void` | 选中/展开会话 | 未读时置 `unread: false` | 无 |
| `forgetThread` | `(id) => void` | 404、删除、永久删除 | 中止监控与历史、删除缓存与阅读位置、过滤线程与语音记录 | 无 |
| `saveSpeech` | `(messageId, request) => void` | 语音组件 | 合并进 `speech` 并持久化 | 无 |

### R6. 运行监控：`watch()` / `applySnapshot()` / `applyEvent()`

- 定位与签名：`WorkspaceStore.watch(id: string, runId: string): void`（启动异步循环，不返回 Promise）；`applySnapshot(id, run: RunSnapshot)`、`applyEvent(id, event: ServiceEvent)` 为私有方法。
- 调用方与条件：R3/R5 在发现未终态运行或新提交后调用；`monitors` 以线程 id 为键，同一 `runId` 不重复启动。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `runId` | `string` | `thread.run.id` / `accepted.run_id` | 必填 | 快照与事件地址 |
| `after` | `number` | 本地缓存 `cache.threads[id].after`，且 `cache.runId === runId` | 不匹配时为 0 | SSE 续传序号 |
| `signal` | `AbortSignal` | `AbortSignal.any([controller, lifecycle])` | 必填 | 取消 |

功能与内部调用（循环体）：
1. `api.snapshot(runId, signal)` + `api.state(id, signal)` → `applySnapshot` / `updateThread({ state })`。
2. `terminal(snapshot)` 时 `loadMemoryStatus(id)` 并结束循环。
3. 计算 `after`：本地缓存 `runId` 与当前 `runId` 一致时取 `Math.min(saved.after, snapshot.last_event_sequence)`，否则从 0 开始；随后调用 `api.events(runId, after, signal, emit)`；回调内 `applyEvent`，成功一次把 `failures` 清零。
4. 捕获异常：写入 `syncError: '同步暂时中断，正在恢复：…'`；404 直接结束；否则 `failures++` 后 `delay(Math.min(10_000, 500 * 2 ** Math.min(failures, 5)), signal)` 退避重试。

`applySnapshot` 的输出：`run`、`phase = runPhase(run)`、按稳定 ID 合并消息（`mergeMessages` 以 id 去重并按 `sequence` 排序）、未读检测（新 assistant 消息）、`error` 文案（`interrupted/failed/completed_with_warnings` 三种），`syncError` 清空。

`applyEvent` 的分支：

| 事件类型 | 处理 | 状态变化 |
| --- | --- | --- |
| `message.committed` | `messageDto.parse(event.payload.message)` → `toMessage` | 合并消息、必要时置 `unread` |
| `state.updated` | `stateDto.parse(event.payload)` | `state = { ...thread.state, ...state }` |
| `memory.retrieved` | `z.array(hitDto).parse(event.payload.hits)` | 按 `hit.id` 去重合并 `state.retrieved_memories` |
| `phase` / `run.started` | `runPhase({ ...thread.run!, phase: String(event.payload.phase), status: 'running' })` | 更新 `phase`，清 `syncError` |
| 其它（`run.completed` / `run.failed`） | 不直接改线程 | 仅推进 `after` |

去重规则：若 `cache.threads[id].runId === event.run_id && event.sequence <= cache.after`，整条事件忽略；处理完成后把 `{ runId, after: event.sequence }` 写回缓存并 `persist()`。

### R7. 模式切换与检查点同步

| 方法 | 签名 | 输入与前置条件 | 行为与输出 |
| --- | --- | --- | --- |
| `switchMode` | `(mode: 'demo' \| 'service', baseUrl = 'http://127.0.0.1:8765') => Promise<void>` | 设置页“连接本机服务/切换到演示” | 构造新适配器 → `dispose()` + 新 `lifecycle` → `restore()` → 写 `CONNECTION_KEY` → `publish({}, false)` → `initialize()` |
| `previewCheckpointSync` | `(id: string) => Promise<CheckpointSyncResult>` | 服务模式；会话非执行中 | 先 `api.thread(id)` 合并最新版本，再 `api.syncCheckpoint(id, current.version ?? 1, true)`（dry-run）；演示模式抛“演示模式不支持重新加载检查点。” |
| `applyCheckpointSync` | `(id, preview: CheckpointSyncResult) => Promise<CheckpointSyncResult \| undefined>` | 预览后确认 | `api.syncCheckpoint(id, preview.version, false, preview.checkpoint_id)`；若本地版本已更新则保留本地线程，否则 `resetHistory` 并写入新 `version/state`；再取一次线程并 `loadHistory` |

`dispose()` 的行为：`epoch++`、中止 `lifecycle` 与全部监控/历史控制器、清空 `running`、`clearInterval(timer)`、复位 `initialized/refreshing`。`resetHistory` 会中止该线程的监控与历史请求、清空消息、重置分页游标与阅读位置。

### R8. 设置、档案与阅读位置相关方法

| 方法 | 签名 | 输入与前置条件 | 行为与输出 |
| --- | --- | --- | --- |
| `loadHistory` | `(id, more = false) => Promise<void>` | 服务模式且线程存在；`more` 需要 `historyCursor` | 并行 `api.messages(id, before)` + `api.state(id)`，按 id 合并消息并写 `state`、`historyCursor`、`historyLoaded`；最后若存在运行则 `watch` |
| `loadMemoryStatus` | `(id) => Promise<void>` | 服务模式 | `api.memoryStatus(id)` → `memoryStatus`；失败写 `memoryStatusError`，不影响聊天 |
| `saveProfile` | `(id, language, text, version?) => Promise<void>` | 角色档案编辑 | 演示模式写 `profileOverrides`；服务模式要求 `version`，`api.saveProfile` 后替换 `characters` 中的角色 |
| `reloadCharacter` | `(id) => Promise<Character \| undefined>` | 版本冲突后“读取最新版本” | `api.character(id)` 并替换；演示模式直接返回 |
| `setReading` | `(id, position: ReadingPosition) => void` | `MessageList` 滚动/挂载 | 写入 `reading` 并 `persist()` |

`restore()`、`persist()`、`publish()` 的关系：`publish(patch, persist = true)` 先合并快照再按需持久化，最后通知全部 `listeners`；服务模式的 `persist()` 只保存 `draft/unread/pending/unsent/runId/after` 等恢复标识，不保存消息正文。

### R9. `useWorkspace()`

- 定位与签名：`useWorkspace(): Workspace`；`useSyncExternalStore(workspaceStore.subscribe, workspaceStore.getSnapshot)`。
- 调用方与条件：所有页面组件。
- 输入：无参数；隐式依赖模块级单例 `workspaceStore`。
- 输出：不可变快照；`subscribe` 返回取消订阅函数。
- 边界：`getSnapshot` 必须返回同一引用，`WorkspaceStore` 只在 `publish`/构造时替换 `this.value`。

## 分支与异常链

1. **本地存储不可用或损坏**：`restore()` 捕获异常后把键加入 `blockedKeys`，本次运行不再写入缓存；`storageWarning` 在全局提示条显示。`persist()` 写入失败时把 `storageWarning` 更新为“本地存储不可用或已满…”。
2. **服务连接失败**：`refresh()` 的 `catch` 在未取消时 `publish({ connection: 'disconnected', connectionError, loading: false })`；`App` 显示服务提示条并提供“重新连接”，不会自动切换到演示。
3. **会话 404**：`refresh()` 中 `ApiError.status === 404` → `forgetThread(id)`；`watch()` 中 404 直接结束监控。
4. **版本回退的轮询结果**：`mergeRemoteThread` 在 `current.version < old.version` 时返回旧对象，避免慢轮询覆盖刚提交的同步结果。
5. **发送结果不确定**：`submitPending` 遇到 `uncertain` 的 `ApiError` 时保留 `pending` 与请求键，提示“发送结果尚未确认…不会重复新增输入”；刷新后 `refresh()` 会重新提交同一请求键。
6. **线程忙碌**：`thread_busy` 错误携带 `details.run_id` 时直接 `watch` 该运行，不丢弃用户输入。
7. **重复初始化**：`StrictMode` 双调用被 `initialized` 守卫拦截；`initializeWorkspace` 在桌面探测失败时静默回退 `initialize()`。
8. **简化模式切换失败**：`toggleCompact` 捕获后由 `reportError` 写入 `platformError`，`compact` 保持原值。
9. **页面关闭**：`beforeunload` 触发 `dispose()`，中止所有在途请求与轮询。

## 输入输出示例

**示例 1（R3，服务模式成功刷新）**

输入：`GET /api/health` 返回 `{"service":"mybot","status":"ok"}`；`GET /api/ready` 返回 `{"status":"ready","database":"connected","capabilities":{...}}`；`GET /api/characters` 返回 `[{"id":"SuLi"}]`。

输出（`publish` 合并的增量，节选）：

```json
{
  "connection": "connected",
  "connectionError": "",
  "ready": { "status": "ready", "database": "connected", "capabilities": { "chat": true } },
  "characters": [{ "id": "SuLi", "name": "SuLi", "profiles": {}, "assets": [] }],
  "loading": false
}
```

**示例 2（R5/R6，发送与恢复标识）**

`send('demo-thread', false)` 在服务模式写入的 `pending`：

```json
{ "requestId": "b1f6…", "text": "雨停了吗？" }
```

刷新后 `refresh()` 发现该 `pending` 并调用 `submitPending`，使用同一 `requestId` 调 `POST /api/threads/{id}/runs`，服务端按 `(thread_id, client_request_id)` 幂等，不新增一轮输入。

**示例 3（R6，SSE 事件去重）**

缓存 `{ runId: "run-1", after: 7 }` 时收到 `{ run_id: "run-1", sequence: 7, type: "phase", ... }`，`applyEvent` 直接返回；收到 `sequence: 8` 才处理并把 `after` 更新为 8。

## 关联文档与验证依据

- 上级：[frontend/README.md](../README.md)
- 同级：[api/README.md](../api/README.md) · [chat/README.md](../chat/README.md) · [characters/README.md](../characters/README.md) · [memory/README.md](../memory/README.md) · [conversations/README.md](../conversations/README.md) · [settings/README.md](../settings/README.md) · [platform/README.md](../platform/README.md) · [design/README.md](../design/README.md)
- 协作模块：[server/README.md](../../server/README.md)（HTTP/SSE 契约）· [cli/README.md](../../cli/README.md)（共用会话与记忆策略）
- 服务接口清单：[frontend/API_CONTRACT.md](../../../frontend/API_CONTRACT.md)
- 单元测试：[store.test.ts](../../../frontend/src/app/store.test.ts)（输入保真、防重复发送、失败重试、中断恢复、损坏数据不覆盖）、[service-store.test.ts](../../../frontend/src/app/service-store.test.ts)（发送与排队区分、后台记忆不阻塞、旧 CLI 扫描、丢失回执恢复、SSE 去重）、[checkpoint-sync.test.ts](../../../frontend/src/app/checkpoint-sync.test.ts)（同步后重置分页/阅读位置、过期响应丢弃、冲突后重新预览）
- 端到端测试：[workspace.spec.ts](../../../frontend/e2e/workspace.spec.ts)、[service.spec.ts](../../../frontend/e2e/service.spec.ts)
- 历史验收记录（原 `frontend/README.md`，已迁移）：2026-09-21 生产构建、16 项单元测试、9 项演示浏览器流程、6 项实际 API 浏览器流程通过；2026-09-22 生产构建、19 项单元测试、9 项实际 API 浏览器流程通过，桌面程序通过正式对话、简化模式、历史恢复及正常退出测试。记忆与模型使用受控测试实现。
- 未验证项：本页内容为静态阅读源码与既有测试整理，本次未重新执行测试；桌面窗口行为、真实模型与真实 TTS 权重未在本轮验证。
