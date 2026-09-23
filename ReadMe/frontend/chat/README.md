# 对话工作台与简化模式（chat/、compact-chat/）

## 职责与入口

本页覆盖两个功能目录：`features/chat/`（工作台对话：消息流、输入、状态、情境面板、语音）与 `features/compact-chat/`（简化模式的悬浮输入条、气泡与几何计算）。两者都是 React 组件模块，不是 Agent 图节点；组件的调用链是“父组件渲染 → 用户事件 → `WorkspaceStore` 方法”，而不是图节点调度。

| 组件 | 文件 | 类型 |
| --- | --- | --- |
| `ChatPage`、`RunStatus`、`StateBlock` | [ChatPage.tsx](../../../frontend/src/features/chat/ChatPage.tsx) | 页面与状态展示组件 |
| `Composer` | [Composer.tsx](../../../frontend/src/features/chat/Composer.tsx) | 输入组件 |
| `MessageList` | [MessageList.tsx](../../../frontend/src/features/chat/MessageList.tsx) | 消息流组件（阅读位置、分页、未读） |
| `SpeechPlayer` | [SpeechPlayer.tsx](../../../frontend/src/features/chat/SpeechPlayer.tsx) | 语音任务组件 |
| `CompactChat` | [CompactChat.tsx](../../../frontend/src/features/compact-chat/CompactChat.tsx) | 简化模式页面 |
| `clamp`、`resizeRect`、`Direction`、`Rect` | [geometry.ts](../../../frontend/src/features/compact-chat/geometry.ts) | 纯函数与类型 |

- **入口 1**：`App` 在 `page === 'chat'` 时渲染 `ChatPage`，见 [app 叶子](../app/README.md) R4；`ChatPage` 再渲染 `MessageList`、`Composer`。
- **入口 2**：`App` 在 `compact && thread` 时改为渲染 `CompactChat`，`CompactChat` 复用同一 `Composer`、`MessageList` 与 `RunStatus`。
- **上游**：`App`、`WorkspaceStore`；**下游**：`WorkspaceStore` 的 `send/setDraft/loadHistory/markRead/setReading/saveSpeech/recover/restoreUnsent`，以及 [platform 叶子](../platform/README.md) 的窗口能力。
- **触发时机**：页面导航、输入、滚动、点击、窗口焦点变化、SSE 驱动的快照更新。

## 调用链总览

组件级链路（编号稳定，后文按编号展开）：

```text
R1 ChatPage 挂载/重渲染：读 workspace 快照 → 渲染会话区（元信息/策略/历史提示/立绘/MessageList/RunStatus/Composer）
   └─ inspector：状态标签（StateBlock ×3）或记忆标签（检索命中按钮）
R2 RunStatus：根据 phase/pending/error/memoryStatus 渲染状态文案与恢复/重试入口
R3 Composer：草稿与可用性 → submit() → onSend()（工作台用于 follow token，简化模式用于回到最新）→ workspaceStore.send()
R4 MessageList：挂载与滚动 → workspaceStore.reading / setReading；历史分页 → workspaceStore.loadHistory()
R5 SpeechPlayer：生成或恢复语音任务 → api.speech/speechJob → workspaceStore.saveSpeech()
R6 CompactChat：展开/收起、拖拽/缩放、native 尺寸适配；内部复用 R2~R4
R7 geometry.resizeRect()：浏览器预览与测试使用的矩形缩放算法
```

## 构建链

本目录没有工厂函数或模块级初始化副作用：所有组件都是函数组件，由父组件在渲染时直接实例化；`geometry.ts` 只有纯函数与类型；`CompactChat.tsx` 顶层仅有常量 `directions` 与 `labels`。因此不存在“构建阶段”与“运行阶段”的参数分离，下文直接按组件挂载与事件链展开。

## 运行链

### R1. `ChatPage` 渲染

- 定位与签名：`ChatPage({ thread, focus, inspectorVisible, openMemory }: { thread: Thread; focus: boolean; inspectorVisible: boolean; openMemory: (id: string) => void })`。
- 调用方与条件：`App` 在 `page === 'chat' && thread` 时渲染；`thread` 变化或快照更新会重渲染。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `thread` | `Thread` | `App` 的活动线程 | 必填 | 消息、草稿、策略、运行状态 |
| `focus` | `boolean` | `App.focus` | 必填 | 专注阅读时隐藏 inspector（CSS `without-inspector`） |
| `inspectorVisible` | `boolean` | `App.inspectorVisible` | 必填 | 是否渲染情境面板 |
| `openMemory` | `(id) => void` | `App` 闭包 | 必填 | 点击检索命中后跳到记忆页并打开详情 |
| `tab` | `'state' \| 'memory'` | `useState('state')` | 默认状态 | inspector 标签 |
| `followToken` | `number` | `useState(0)`，`Composer.onSend` 自增 | 0 | 发送后让 `MessageList` 跟随到底部 |
| `preferences.sprites[thread.characterId]` | `SpriteSettings` | `useWorkspace()` | 未配置时 `undefined` | 立绘开关、素材、位置、缩放、镜像 |
| `characters` / `mode` | `Character[]` / `'demo' \| 'service'` | `useWorkspace()` | 必填 | 解析立绘素材、分支展示 |

功能与内部调用：
1. `useEffect(() => workspaceStore.markRead(thread.id), [thread.id, thread.unread])` 清除未读。
2. `hasExampleState = mode === 'demo' && thread.id === 'demo-suli'`：仅内置演示会话显示虚构情境与示例记忆。
3. 顶部渲染 `conversation-meta`（当前会话/新对话 + 模式说明）、服务模式的长期记忆策略、`thread.historyNotice`。
4. `conversation-body`：若 `sprite.enabled` 且素材存在，渲染 `sprite-stage` 的 `<img>`（`height: scale%`、`mirror` 时 `scaleX(-1)`）；随后渲染 `<MessageList key={thread.id} thread={thread} followToken={followToken} />`。
5. `thread.pending` 时渲染“正在确认发送的输入”折叠块；随后 `<RunStatus thread={thread} />` 与 `<Composer thread={thread} onSend={() => follow(v => v + 1)} />`。
6. inspector 记忆标签：服务模式使用 `state?.retrieved_memories`，演示示例使用 `demoMemories.slice(0, 2)`，每条按钮调用 `openMemory(memory.id)`；无数据时显示提示。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `followToken` | `number` | 发送成功调用 `onSend` | 自增 | `MessageList` 恢复跟随 |
| `tab` | `'state' \| 'memory'` | 点击标签 | 覆盖写 | inspector 分支 |
| `markRead` 副作用 | 状态更新 | 挂载或未读变化 | `thread.unread = false` | 会话列表、简化模式未读提示 |

`StateBlock({ icon, title, rows })`：`rows: string[][]`，渲染 `<section class="state-block">` 的 `h3` 与 `dl`；缺失值由调用方用 `display()` 转成“未记录”。

### R2. `RunStatus`

- 定位与签名：`RunStatus({ thread }: { thread: Thread })`；同文件导出，`CompactChat` 也复用它。
- 调用方与条件：`ChatPage` 与 `CompactChat` 的消息区底部。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `thread.phase` / `thread.pending` | `Thread['phase']` / `PendingInput?` | 快照 | 默认 `'idle'` | 决定忙碌与文案 |
| `thread.syncError` | `string?` | `watch`/`loadHistory` 写入 | 可选 | 优先显示同步提示与“恢复同步” |
| `thread.error` | `string?` | `applySnapshot` 等写入 | 可选 | 失败/警告文案与重试按钮 |
| `thread.memoryPolicy.storage` / `thread.memoryStatus` / `thread.memoryStatusError` | `boolean?` / `MemoryStatus?` / `string?` | 快照 | 仅服务模式且开启存储时显示 | 后台记忆状态 |
| `thread.unsent` | `string?` | 发送被拒时写入 | 可选 | “有未发送的文字”恢复入口 |

功能与内部调用：
1. `busy = isBusy(thread) || !!thread.pending`；`memoryLabels` 映射 `pending/running/failed/completed` 的中文说明。
2. 文案优先级：`syncError` → 忙碌时 `phaseLabels[thread.pending && !isBusy(thread) ? 'sending' : thread.phase]` → `thread.error` → 已完成/等待第一条消息。
3. 服务模式且开启存储时追加后台记忆状态；`syncError || pending` 显示“恢复同步”（`workspaceStore.recover`）；`unsent` 显示折叠块与“恢复到输入框”（`restoreUnsent`，草稿非空时禁用）。
4. `phase === 'error' && !pending && (mode === 'demo' || run.status ∈ {failed, interrupted})` 显示重试按钮（`workspaceStore.send(id, true)`）。

输出：状态文本与按钮；副作用为调用 `recover`/`restoreUnsent`/`send(retry)`。

### R3. `Composer`

- 定位与签名：`Composer({ thread, compact = false, expanded = true, children, prefix, onSend }: {...})`。
- 调用方与条件：`ChatPage`（工作台）与 `CompactChat`（简化模式）。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `thread` | `Thread` | 父组件 | 必填 | 草稿、角色名、忙碌状态 |
| `compact` | `boolean` | 父组件 | 默认 `false` | 简化模式布局（单行、圆角、头像） |
| `expanded` | `boolean` | `CompactChat.expanded` | 默认 `true` | 简化模式是否展开消息 |
| `children` | `ReactNode` | `CompactChat` 的未读标签与按钮 | 可选 | 输入条尾部附加内容 |
| `prefix` | `ReactNode` | `CompactChat` 的拖动把手 | 可选 | 输入条头部 |
| `onSend` | `() => void` | 父组件闭包 | 可选 | 发送成功后的附加动作 |
| `mode` / `connection` / `ready` | `Workspace` 字段 | `useWorkspace()` | 必填 | 计算 `unavailable` |

功能与内部调用：
1. `busy = isBusy(thread) || !!thread.pending`；`unavailable = mode === 'service' && (connection !== 'connected' || !ready?.capabilities.chat)`。
2. `useLayoutEffect` 调整 textarea 高度：基础 `compact ? 36 : 60`；`!compact || expanded` 时按 `Math.min(compact ? 120 : 180, scrollHeight)` 增高，否则 `overflowY: 'hidden'`。
3. `submit()`：`busy || unavailable || !thread.draft.trim()` 直接返回；否则先 `onSend?.()`，再 `void workspaceStore.send(thread.id)`；工作台或展开状态把焦点还给输入框。
4. `onSubmit` 阻止默认提交；`onKeyDown`：`Enter && !shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229` 时阻止默认并 `submit()`（中文输入法组合期间不发送）。
5. `onChange` → `workspaceStore.setDraft(thread.id, event.target.value)`；`maxLength = 50000`。
6. 发送按钮 `disabled = busy || unavailable || !draft.trim()`；`unavailable` 时 `title` 提示“对话服务尚未就绪，草稿已保留”。

输出：调用 `send`（见 [app 叶子](../app/README.md) R5）；草稿本身保存在 store。异常与边界：纯空白不发送；`trim` 只用于判断，不修改发送文本。

### R4. `MessageList`

- 定位与签名：`MessageList({ thread, compact = false, height, visible = true, followToken = 0 }: {...})`。
- 调用方与条件：`ChatPage`（完整阅读）与 `CompactChat`（`compact`、`visible={expanded}`、可选 `height`）。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `thread.messages` | `Message[]` | 快照 | 必填 | 渲染消息 |
| `compact` | `boolean` | 父组件 | 默认 `false` | 简化模式隐藏头像与时间，使用气泡样式 |
| `height` | `number?` | `CompactChat.layout.history`（浏览器预览） | 桌面 native 时不传 | 消息区高度 |
| `visible` | `boolean` | `CompactChat.expanded` | 默认 `true` | 收起时不做滚动恢复 |
| `followToken` | `number` | `ChatPage`/`CompactChat` | 0 | 变化时强制跟随到底部 |
| `mode` | `'demo' \| 'service'` | `useWorkspace()` | 必填 | 分页按钮与语音播放器显隐 |

功能与内部调用：
1. 初始化 `following = workspaceStore.reading.get(thread.id)?.following ?? true`。
2. `useLayoutEffect`（依赖 `thread.id/messages.length/height/visible/followToken`）：
   - 读取 `reading.get(thread.id)`；`followToken` 变化时把 `saved.following` 置真；
   - 跟随模式滚动到底部，否则滚到 `saved.top`，并按 `anchorId + offset` 修正锚点；
   - 计算 `pending`（非跟随时最后一条消息 id 或数量变化）并 `setReading` 回写 `top/following/count/lastId`；
   - 用 `ResizeObserver` 观察容器与每条 `[data-message-id]`，尺寸变化时重复上述恢复（覆盖原生窗口缩放与多行输入增高）。
3. `onScroll`：恢复中或不可见时忽略；视口尺寸变化（`viewportSize` 不匹配）时忽略；`atBottom = scrollHeight - clientHeight - scrollTop <= 24`；找到首个底部超过容器顶部的消息作为锚点，`setReading` 写入 `anchorId/offset/lastId`；`setFollowing(atBottom)`，到底部时清 `pending`。
4. 顶部：服务模式且 `historyCursor || !historyLoaded` 时显示“读取消息历史/加载更早消息”（`workspaceStore.loadHistory(id, !!historyCursor)`，`historyLoading` 时禁用）。
5. 列表：空消息显示角色名与模式提示；每条 `<article data-message-id>` 渲染作者与时间（演示标“演示”、`timestampEstimated` 标“旧历史 · 时间为估计值”、否则本地时间）；assistant 且服务模式时渲染 `<SpeechPlayer messageId={message.id} />`。
6. 底部：`!following` 时显示“回到最新/有新消息 · 回到最新”（`jump()` 滚到底并写回阅读位置）。

输出：`workspaceStore.reading` 中的 `ReadingPosition`（`top/following/count/anchorId/offset/lastId`）。副作用：滚动容器；调用 `loadHistory`。

### R5. `SpeechPlayer`

- 定位与签名：`SpeechPlayer({ messageId }: { messageId: string })`；仅服务模式渲染。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `messageId` | `string` | `MessageList` | 必填 | 语音任务键 |
| `speech[messageId]` | `SpeechRequest \| undefined` | 快照 | 无记录时显示“生成语音” | 请求键与任务状态 |
| `ready.capabilities.speech` | `boolean` | 快照 | 未就绪时禁用按钮 | 能力开关 |
| `playError` / `reconnect` | `string` / `number` | `useState` | 空 / 0 | 音频读取失败与重连 |

功能与内部调用：
1. `useEffect`（依赖 `api/messageId/request?.requestId/reconnect`）：无请求或非服务模式直接返回；否则建立 `AbortController`。
2. 首次：`request.job ? api.speechJob(request.job.id, signal) : api.speech(messageId, request.requestId)`。
3. 循环：`workspaceStore.saveSpeech(messageId, { requestId, job })`；若 `job.status` 不是 `queued/running` 则结束；否则等待 1500ms 后 `api.speechJob(job.id)` 继续；`current()` 校验组件仍挂载、适配器未切换、`requestId` 未变。
4. 异常：`saveSpeech` 写入 `error`，界面显示“恢复语音任务”。
5. `start()`：若已有 `error` 且不是已失败任务，则清除 `error` 并 `reconnect++`（复用原请求键，避免重复生成）；否则写入新的 `requestId`（`crypto.randomUUID()`），表示重新生成。
6. 播放：`api.resource(request.job.resource_url)` 成功且无 `playError` 时渲染 `<audio controls preload="none">`；`onError` 显示“音频资源读取失败，可重新生成。”。
7. 按钮文案：`busy` → “正在生成语音…”；`request.error` → “恢复语音任务”；`failed || playError` → “重试语音”；否则“生成语音”。

输出：`speech` 持久化记录；副作用为网络轮询与音频元素。边界：演示模式返回 `null`；`ready.capabilities.speech` 为假时禁用。

### R6. `CompactChat`

- 定位与签名：`CompactChat({ thread, leave, onError }: { thread: Thread; leave: () => void; onError: (error: unknown) => void })`；由 `App` 在 `compact && thread` 时渲染。
- 状态与引用：`expanded`、`dragging`、`followToken`、`composerHeight`、`layout { left, bottom, width, history }`；`group/stage/composer` 容器引用，`drag` 记录指针拖拽，`insidePointer` 区分内部按下，`nativeReady/nativeFitTicket` 防止原生适配竞态。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `preferences.compactWidth` / `historyHeight` | `number` | 偏好 | 默认 660 / 280 | 初始窗口与消息区尺寸 |
| `preferences.replyPlacement` | `'bubble' \| 'inline'` | 偏好 | 默认 `bubble` | 消息区在输入条上方或内部上沿 |
| `preferences.alwaysOnTop` | `boolean` | 偏好 | 默认 `true` | 进入简化模式时置顶 |
| `desktop` | `boolean` | platform | 必填 | 原生窗口与浏览器预览分支 |
| `thread.unread` | `boolean` | 快照 | 必填 | 收起时显示“有新回复” |

功能与内部调用（按交互链）：
1. **挂载**：`saveSize()` 把 `layout.width/history` 按 `320–2400 / 72–1600` 钳制后写入偏好；桌面模式另有 200ms 防抖保存。
2. **展开**：`onFocusCapture`/`onPointerDown` 命中 `HTMLTextAreaElement` 时 `setExpanded(true)`；展开且页面可见、窗口有焦点时 `markRead`。拖动把手、返回按钮、角色标识、重新激活窗口都不展开。
3. **收起**：`pointerdown` 落在焦点组外、`window.blur`、`visibilitychange` 隐藏、`onNativeFocus(false)`、焦点移出 `compact-group` 时调用内部 `blur()`：结束拖拽、让 textarea 失焦、`setExpanded(false)`；`Escape` 同样收起。收起保留草稿与阅读位置。
4. **原生尺寸适配**：桌面模式下用 `getComputedStyle` 计算 `.compact-history` 的 padding/border/margin、`.compact-heading` 与 `.run-status` 的高度、`.compact-surface` 边框与 `composerHeight`，得到 `chrome`；调用 `fitCompact(expanded ? history + chrome + 16 : inputHeight + 16, expanded ? 72 + chrome + 16 : inputHeight + 16)`；用 `nativeFitTicket` 丢弃过期结果后 `syncNativeDimensions()` 回读窗口宽度与消息区高度。
5. **拖拽/缩放**：`begin(event, direction | 'move')` 只响应左键；桌面模式调用 `dragNative()` / `resizeNative(direction)`，浏览器模式记录 `start/bounds/chrome` 并 `setPointerCapture`；`move` 对 `'move'` 做位置钳制，对方向调用 `resizeRect(start, direction, dx, dy, bounds, 320, chrome + (expanded ? 72 : 0))`，再换算成 `{ left, bottom, width, history }`；`endDrag` 释放捕获并保存尺寸。缩放边缘：展开时渲染 8 个方向，收起时只渲染 `e/w`（左右调宽）；浏览器下方向键可按 8px 步进调整宽高。
6. **浏览器预览约束**：`ResizeObserver` 观察 `stage`，`contain()` 把 `width/left/history/bottom` 限制在场景内（消息区最低 72px）；展开或输入条增高后再钳制一次。
7. **渲染**：`compact-group` 带 `replyPlacement/expanded/dragging` 类；`bubble` 时消息区渲染在输入条上方，`inline` 时渲染在 `compact-surface` 内部上沿；`Composer` 以 `compact`、`expanded` 复用，`prefix` 为拖动把手，`children` 包含未读提示、“返回工作台”（`leave`）与桌面版的“退出程序”（`closeDesktop`）。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `expanded` | `boolean` | 输入聚焦/Escape/失焦 | 覆盖写 | 消息区显隐、原生高度 |
| `layout.width` / `layout.history` | `number` | 拖拽、原生回读、浏览器钳制 | 写偏好 `compactWidth/historyHeight` | 窗口尺寸、设置页显示 |
| `followToken` | `number` | 发送后 | 自增 | `MessageList` 跟随 |

### R7. `geometry.ts`

| 函数 | 签名 | 输入 | 输出与规则 |
| --- | --- | --- | --- |
| `clamp` | `(value, min, max) => number` | 三个数值 | `Math.max(min, Math.min(max, value))` |
| `resizeRect` | `(start: Rect, direction: Direction, dx, dy, bounds: Rect, minWidth, minHeight) => Rect` | 起始矩形、方向、位移、边界、最小宽高 | 所有值使用 CSS 像素；`e` 增宽、`w` 减宽并同步 `x`、`s` 增高、`n` 减高并同步 `y`；宽高同时受 `Math.min(minWidth, bounds.width)` 与边界限制；对边在最小尺寸时仍保持固定 |

`Direction = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw'`；`Rect = { x, y, width, height }`。

## 分支与异常链

1. **输入法组合**：`Composer` 的 Enter 判定排除 `isComposing` 与 `keyCode === 229`，组合期间只换行不发送。
2. **忙碌/未就绪**：`Composer` 在 `busy` 或 `unavailable` 时禁用发送并保留草稿；`RunStatus` 显示阶段文案。
3. **同步中断**：`syncError` 存在时 `RunStatus` 优先显示并提供“恢复同步”，调用 `recover`（有 `pending` 先确认发送，否则重新监控并读历史）。
4. **失败与中断**：`phase === 'error'` 时按运行状态显示重试；已提交但后处理异常（`completed_with_warnings`）只提示不重生成。
5. **历史分页**：`MessageList` 顶部按钮触发 `loadHistory`；加载期间禁用；返回后按消息 id 合并并保持阅读锚点（见 [app 叶子](../app/README.md) R8）。
6. **新消息与未读**：非跟随状态下 `pending` 为真时按钮显示“有新消息 · 回到最新”；简化模式收起时只显示“有新回复”，不弹出正文、不抢焦点。
7. **语音**：生成失败或音频读取失败只影响该条消息的语音入口，可单独重试；响应丢失时复用原请求键，明确失败的任务重新生成。
8. **简化模式失焦**：内部按钮点击、复制、拖动不会误收起；真正离开焦点组或窗口失焦才隐藏消息，草稿与阅读位置保留。
9. **尺寸边界**：浏览器预览最小宽度 320、消息区最小高度 72、最大高度 1600（偏好钳制）；原生窗口另有 `fitCompact` 的工作区边界限制。
10. **窗口切换竞态**：`App.toggleCompact` 退出时先 `flushSync` 收起再放大窗口，避免工作台尺寸覆盖浮窗尺寸；`nativeFitTicket` 丢弃过期原生适配结果。

## 输入输出示例

**示例 1（R3，输入与发送）**

草稿 `"去上次那家书店吧。\n"`，`busy = false`，按 Enter：

- `onKeyDown` 命中条件 → `submit()` → `onSend()`（`followToken + 1`）→ `workspaceStore.send('demo-suli')`；
- 服务模式下 store 写入 `pending`，`Composer` 的按钮在收到快照后变为忙碌图标；
- `trim` 只用于判断，实际发送文本保留换行与缩进。

**示例 2（R4，阅读位置）**

用户上翻后 `onScroll` 写入：

```json
{ "top": 812, "following": false, "count": 4, "anchorId": "m-3", "offset": -6, "lastId": "m-3" }
```

新消息到达后按钮显示“有新消息 · 回到最新”；点击 `jump()` 后写回 `{ "following": true, "count": 5, "lastId": "m-5" }`。

**示例 3（R6，浏览器预览拖拽左上角）**

`start = { x: 100, y: 200, width: 660, height: 344 }`、`bounds = { x: 14, y: 14, width: 1200, height: 700 }`、`chrome = 64`、`expanded = true`、`dx = -50`、`dy = -30`：

- `resizeRect` 返回 `{ x: 50, y: 170, width: 710, height: 374 }`（左边与上边移动，右边与下边固定）；
- 换算后 `layout = { left: 50, bottom: 场景高 - 170 - 374, width: 710, history: 374 - 64 }`。

## 关联文档与验证依据

- 上级：[frontend/README.md](../README.md)
- 同级：[app/README.md](../app/README.md)（`send`/`reading`/`speech`/`recover`）· [api/README.md](../api/README.md)（HTTP/SSE）· [platform/README.md](../platform/README.md)（`fitCompact`/`dragNative`/`resizeNative`）· [design/README.md](../design/README.md)（简化模式交互规范）· [settings/README.md](../settings/README.md)（偏好字段）
- 服务端事件与历史：[server/README.md](../../server/README.md)
- 单元测试：[geometry.test.ts](../../../frontend/src/features/compact-chat/geometry.test.ts)（最小尺寸锚点、工作区约束、窄预览不横向溢出）
- 端到端测试：[workspace.spec.ts](../../../frontend/e2e/workspace.spec.ts)（IME Enter、Shift+Enter 换行、后台回复隐藏、边沿/四角缩放与最小高度、仅输入框展开、记忆浏览与立绘）、[service.spec.ts](../../../frontend/e2e/service.spec.ts)（历史分页锚点、后台记忆、语音生成与加载、真实状态无演示示例）
- 历史验收记录（原 `frontend/README.md`，已迁移）：2026-09-21 桌面回归覆盖两种回复布局、真实窗口宽高、72px 最小消息区、翻阅位置、跟随最新、多行输入收起、跨模式尺寸保存与工作台尺寸/位置恢复；2026-09-22 覆盖简化模式、历史恢复与退出程序。多显示器混合 DPI、原生鼠标拖拽和实际安装/卸载流程仍需人工验收。
- 未验证项：本页为静态阅读源码与既有测试整理，本次未重新执行测试；真实 TTS 权重合成未验收。
