# 平台适配与桌面壳（platform/、src-tauri/）

## 职责与入口

本页覆盖两个平台层：前端 `src/shared/platform/`（Tauri IPC 封装与浏览器降级）和桌面后端 `src-tauri/src/`（窗口命令与本机 Python 服务生命周期）。前者是浏览器/Tauri 能力适配模块，后者是 Rust 桌面进程；都不是 Agent 图节点。

| 文件 | 类型 | 源码 |
| --- | --- | --- |
| `service.ts` | Tauri 命令封装：查询/启动本机服务 | [service.ts](../../../frontend/src/shared/platform/service.ts) |
| `window.ts` | 窗口控制：桌面探测、IPC 串行队列、简化模式尺寸、拖动/缩放/焦点 | [window.ts](../../../frontend/src/shared/platform/window.ts) |
| `main.rs` | 桌面入口：命令注册、`set_compact_bounds`、退出清理 | [main.rs](../../../frontend/src-tauri/src/main.rs) |
| `service.rs` | `LocalService`：项目根/端口/解释器发现、复用/启动/端口冲突、owner token 关闭、状态轮询 | [service.rs](../../../frontend/src-tauri/src/service.rs) |

- **入口 1**：`store.ts` 的 `initializeWorkspace()` 调用 `getLocalServiceStatus()`（见 [app 叶子](../app/README.md) R1）。
- **入口 2**：`App.toggleCompact` 调用 `enterCompact/leaveCompact`；`CompactChat` 调用 `fitCompact/dragNative/resizeNative/onNativeFocus/closeDesktop`（见 [chat 叶子](../chat/README.md) R6）。
- **入口 3**：设置页调用 `startLocalService()`（见 [settings 叶子](../settings/README.md) R5）。
- **上游**：React 组件与 store；**下游**：Tauri IPC 命令、Windows 窗口 API、`python -u -m server` 子进程。
- **触发时机**：应用启动、简化模式切换/展开、窗口拖动与缩放、设置页轮询、退出程序。

## 调用链总览

```text
B1 window.ts 模块求值：desktop = isTauri()；建立串行队列 queue 与 previous/compactAnchor
B2 service.ts 模块求值：两个 invoke 包装函数
B3 Tauri main()：manage(LocalService::default()) → setup 启动服务 → 注册 3 个命令 → 退出事件 stop

R1 前端 getLocalServiceStatus() → invoke('local_service_status') → LocalService.status()
R2 LocalService.start() → launch()：复用 / 端口冲突 / 启动子进程 / 30 秒就绪轮询
R3 前端 enterCompact(preferences) → 记录工作台状态 → 无边框置顶 → 定位输入条
R4 CompactChat → fitCompact(height, minimumHeight) → invoke('set_compact_bounds') → 校验 + SetWindowPos
R5 dragNative()/resizeNative(direction)/onNativeFocus() → 系统拖动、缩放、焦点事件
R6 leaveCompact() → 保存浮窗锚点 → 还原工作台尺寸/位置/装饰 → 恢复置顶与最小尺寸
R7 stop() → POST /api/service/shutdown（带 owner token）→ 等待退出 → 超时 kill
R8 前端 startLocalService() → invoke('start_local_service') → start()
```

## 构建链

### B1. `window.ts` 模块求值

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `isTauri()` | `boolean` | 必填 | `@tauri-apps/api/core`；`desktop` 常量 |
| `previous` / `compactAnchor` | 模块级变量 | `undefined` | 工作台窗口快照与浮窗底部锚点 |
| `queue` | `Promise<void>` | `Promise.resolve()` | IPC 串行队列 |

功能：`sequential(operation)` 把操作串到 `queue.then(operation)`，并把 `queue` 更新为“吞掉异常后的结果”，保证多次窗口操作按调用顺序执行、单次失败不阻塞后续。输出：模块级单例状态与全部导出函数。

### B2. `service.ts` 模块求值

无副作用；导出接口 `LocalServiceStatus` 与两个函数：

```ts
interface LocalServiceStatus { status: 'idle' | 'starting' | 'connected' | 'failed' | 'stopping'; base_url: string; managed: boolean; error: string | null }
getLocalServiceStatus = () => invoke<LocalServiceStatus>('local_service_status')
startLocalService = () => invoke<void>('start_local_service')
```

### B3. Tauri `main()`

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `LocalService::default()` | `LocalService` | 必填 | `Arc<Mutex<Inner>>`，包含 `ServiceStatus/child/owner/port/closing` |
| `generate_context!()` | 宏 | 必填 | `tauri.conf.json` 配置与权限 |
| 环境变量 | `MYBOT_PROJECT_ROOT` / `MYBOT_API_PORT` / `MYBOT_PYTHON` | 可选 | 见 R2 |

功能与内部调用：
1. `tauri::Builder::default().manage(service::LocalService::default())`。
2. `setup`：`app.state::<LocalService>().start()`（后台线程启动，不阻塞窗口）。
3. `invoke_handler(tauri::generate_handler![set_compact_bounds, service::local_service_status, service::start_local_service])`。
4. `.build(...).expect("mybot desktop failed to start")`；`app.run` 收到 `RunEvent::Exit` 时 `stop()`。

输出：桌面主进程；副作用：启动服务、注册命令、退出清理。

## 运行链

### R1. `getLocalServiceStatus()` 与 `LocalService.status()`

- 定位与签名：前端 `getLocalServiceStatus(): Promise<LocalServiceStatus>`；Rust `#[tauri::command] pub async fn local_service_status(state) -> Result<ServiceStatus, String>`（内部 `spawn_blocking`）。
- 调用方与条件：`initializeWorkspace()`、设置页 2 秒轮询、重试启动后。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| 内部 `status` | `String` | `Inner` | `idle/starting/connected/failed/stopping` | 前端分支 |
| `base_url` | `String` | `format!("http://127.0.0.1:{port}")` | 端口发现结果 | 自动切换服务模式 |
| `managed` | `bool` | 是否由本进程启动 | 复用服务为 `false` | 退出清理与提示 |
| `error` | `Option<String>` | 失败原因 | — | 设置页展示 |

`status()` 的探活逻辑：若 `status == "connected"` 且存在子进程且已退出 → 置 `failed`、`child = None`、错误“服务已退出，请检查服务日志。”；若 `connected` 且没有子进程（复用外部服务）→ `is_mybot(port)` 探活，失败且未在关闭流程时置 `failed`、错误“已有服务已断开，可重试连接或启动。”。返回克隆后的状态。

### R2. `LocalService.start()` / `launch()`

- 定位与签名：`pub fn start(&self)`（同步，立即返回）→ 后台线程 `fn launch(&self)`。
- 调用方与条件：Tauri `setup`、`start_local_service` 命令、设置页重试。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `MYBOT_PROJECT_ROOT` | `OsString` | 进程环境 | 可选 | 首选项目根 |
| 当前目录/可执行文件祖先 | `PathBuf` | `current_dir` / `current_exe` | 自动 | 依次向上查找 |
| `CARGO_MANIFEST_DIR/../..` | `PathBuf` | 编译期常量 | 兜底 | 构建期项目目录 |
| `MYBOT_API_PORT` / `config/.env` | `u16` | 环境或文件 | 默认 8765 | 服务端口 |
| `MYBOT_PYTHON` / `.venv` / `venv` / PATH | `OsString` | 环境与文件系统 | 依次查找 | 解释器 |

功能与内部调用（`launch()` 分支顺序）：
1. `is_mybot(port)` 为真 → 置 `connected`（复用，`managed` 保持 `false`）并返回。
2. `TcpStream::connect_timeout(127.0.0.1:port, 300ms)` 成功但非 mybot → `fail("本机服务端口已被其他程序占用。", false)`。
3. `project_root()` 找不到 → `fail("未找到 Python 服务目录，请设置 MYBOT_PROJECT_ROOT。", false)`。
4. 创建 `data/log/` 并追加打开 `api-service-console.log`；若已在关闭流程则中止。
5. 生成 `owner = uuid::Uuid::new_v4()`；`Command::new(interpreter)` 参数 `-u -m server --port <port>`，工作目录为项目根，环境 `MYBOT_SERVICE_OWNER_TOKEN=<owner>`、`PYTHONIOENCODING=utf-8`，stdin 空、stdout/stderr 指向日志；Windows 追加 `CREATE_NO_WINDOW (0x08000000)`。
6. `spawn()` 成功 → 记录 `child`、`managed = true`；若此刻已在关闭流程则调用 `stop()`；失败 → `fail("服务启动失败，请检查 Python 环境及 data/log/api-service-console.log。", false)`。
7. 就绪轮询：最多 30 秒、每 200ms 检查一次；子进程退出 → `fail("服务已退出，请检查 data/log/api-service-console.log。", true)`；`is_mybot` 成功 → `connected`；超时 → `fail("服务未能及时就绪，请检查数据库连接与服务日志。", true)`。

`fail(message, clean_child)`：需要时 `kill` + `wait` 子进程，未处于关闭流程时置 `failed` 与错误信息。

`is_mybot(port)` 与 `request(...)` 的契约：只做本机小请求，`GET /api/health`，带 `X-Mybot-Client: mybot-desktop`，连接超时 300ms、读写超时 1s；解析 `HTTP/1.1 200` 且 JSON `service == "mybot" && status == "ok"`。不按进程名杀进程、不执行 shell 命令。

### R3. `enterCompact(preferences)`

- 定位与签名：`enterCompact(preferences: Preferences): Promise<void>`；非桌面直接 `Promise.resolve()`。
- 调用方与条件：`App.toggleCompact(true)`。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `preferences.alwaysOnTop` | `boolean` | 偏好 | 默认 `true` | `setAlwaysOnTop` |
| `preferences.compactWidth` | `number` | 偏好 | 默认 660，范围 320–2400 | 窗口宽度 |

功能与内部调用（串行执行）：
1. 保存 `previous = { position: outerPosition, size: innerSize, maximized: isMaximized }`；最大化时先 `unmaximize()`。
2. `setMinSize(LogicalSize(336, 80))` → `setDecorations(false)` → `setShadow(false)` → `setAlwaysOnTop(preferences.alwaysOnTop)`。
3. 取当前显示器 `workArea` 与缩放因子 `scale`；`width = round(min((compactWidth + 16) * scale, area.width))`、`height = round(84 * scale)`；`setSize(PhysicalSize)`。
4. 定位：若已有 `compactAnchor`，放在锚点底部；否则水平居中并距工作区底部 `36 * scale`；最后 `setFocus()`。

输出：无返回；副作用：窗口样式与几何变化。异常：IPC 失败向上抛出，由 `App.reportError` 显示。

### R4. `fitCompact(height, minimumHeight)` 与 `set_compact_bounds`

- 定位与签名：前端 `fitCompact(height: number, minimumHeight: number): Promise<void>`；Rust `#[tauri::command] fn set_compact_bounds(window, x: i32, y: i32, width: u32, height: u32, minimum_height: f64) -> Result<(), String>`。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `height` | `number`（CSS px） | `CompactChat` 计算的 `history + chrome + 16` 或 `inputHeight + 16` | 必填 | 目标窗口高度 |
| `minimumHeight` | `number`（CSS px） | `72 + chrome + 16` 或 `inputHeight + 16` | 必填 | 最小高度约束 |
| 当前 `size/position/scale/workArea` | Tauri 查询 | `fitCompact` 内读取 | 必填 | 保持底边与工作区约束 |

前端功能：先捕获当前 `innerSize/outerPosition/scale/workArea`（Windows 在最小高度提升时可能立即改窗口尺寸，必须先记录底边）；`nextHeight = round(min(height * scale, area.height))`、`width = round(min(size.width, area.width))`；`y = position.y + size.height - nextHeight` 并把 `x/y` 钳制在工作区内；调用 `invoke('set_compact_bounds', { x: round(x), y: round(y), width, height: nextHeight, minimumHeight })`。

Rust 功能：
1. 校验 `window.label() == "main"` 且窗口无装饰，否则返回“Compact bounds require the borderless main window”。
2. 校验宽高非 0、不超过 `i32::MAX`、`minimum_height` 有限且大于 0，否则返回“Invalid compact window dimensions”。
3. `shrinking = height < window.inner_size().height`；`minimum = LogicalSize(336.0, minimum_height)`。
4. 缩小时先 `set_min_size`，避免 OS 在旧位置单独触发一次调整；随后：
   - Windows：`SetWindowPos(hwnd, None, x, y, width, height, SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER | SWP_NOCOPYBITS)`，保持焦点与层级、不复制旧内容；
   - 其它平台：`set_size` + `set_position`。
5. 放大后再 `set_min_size`。

输出：`Ok(())`；异常：任一 Tauri/Win32 调用失败返回字符串错误，前端 `onError` 展示。

### R5. 拖动、缩放与焦点

| 函数 | 签名 | 输入 | 行为 |
| --- | --- | --- | --- |
| `dragNative` | `() => Promise<void>` | 无 | `getCurrentWindow().startDragging()`，由 OS 接管移动 |
| `resizeNative` | `(direction: Direction) => Promise<void>` | `'n'\|'s'\|'e'\|'w'\|'ne'\|'nw'\|'se'\|'sw'` | 映射为 `startResizeDragging('North'…'SouthWest')` |
| `onNativeFocus` | `(callback: (focused: boolean) => void) => Promise<UnlistenFn>` | 回调 | 订阅 `onFocusChanged`；`CompactChat` 在失焦时收起 |

`CompactChat` 在桌面模式用它们实现原生拖动/缩放；浏览器预览使用 `geometry.resizeRect` 指针拖拽，两条路径的尺寸下限一致（宽 320、消息区 72）。

### R6. `leaveCompact()`

功能与内部调用（串行执行）：
1. 读取当前 `outerPosition/innerSize`，写入 `compactAnchor = { x, bottom: position.y + size.height }`，供下次进入时保持输入条屏幕位置。
2. `setAlwaysOnTop(false)` → `setDecorations(true)` → `setShadow(true)` → `setMinSize(LogicalSize(360, 480))`。
3. `previous` 存在时 `setSize(previous.size)`、`setPosition(previous.position)`，若原先最大化则 `maximize()`。
4. `setFocus()`。

输出：无返回；副作用：恢复工作台窗口。边界：非桌面直接返回。

### R7. `stop()`（owner token 关闭）

| 步骤 | 调用 | 输入 | 行为 |
| --- | --- | --- | --- |
| 1 | 取 `child/owner/port` 并置 `closing = true` | — | 后续 `start/launch` 直接返回 |
| 2 | `request(port, "POST", "/api/service/shutdown", Some(owner))` | owner token | 只允许关闭本次启动的服务 |
| 3 | 最多等待 10 秒（每 100ms `try_wait`） | 子进程 | 正常退出则返回 |
| 4 | `child.kill()` + `wait()` | 超时兜底 | 强制结束 |

复用（`managed = false`，无 `child`）时 `stop()` 不发送任何请求，外部服务继续运行。退出事件由 `main()` 的 `RunEvent::Exit` 触发。

### R8. `startLocalService()`

- 定位与签名：前端 `startLocalService(): Promise<void>`；Rust `#[tauri::command] pub fn start_local_service(state)`。
- 输入：无参数；隐式依赖 `LocalService` 单例。
- 行为：`state.start()`；若已在 `starting/connected` 或关闭流程中则忽略。输出：无返回；状态由下一次 `local_service_status` 反映。

### R9. `closeDesktop()`

`closeDesktop(): Promise<void>`：串行调用 `getCurrentWindow().close()`，用于简化模式“退出程序”按钮；不触发展开消息。`CompactChat` 点击时先 `saveSize()` 再调用。

## 分支与异常链

1. **复用已有服务**：`is_mybot` 为真 → `connected` 且 `managed = false`；退出时只关闭自己启动的进程。
2. **端口被其他程序占用**：不接管、不杀进程，直接 `failed` 并提示。
3. **服务启动失败/提前退出/30 秒未就绪**：分别给出错误文案；需要时清理子进程。
4. **复用服务断开**：`status()` 探活失败后置 `failed`，前端可重试启动。
5. **窗口命令竞态**：`sequential` 保证按序执行；`CompactChat` 用 `nativeFitTicket` 丢弃过期适配结果；`App` 退出简化模式时先 `flushSync` 收起布局再放大窗口。
6. **非主窗口/有装饰窗口**：`set_compact_bounds` 拒绝执行，防止误改其它窗口。
7. **非 Windows**：`set_compact_bounds` 使用 Tauri 的 `set_size/set_position` 兜底。
8. **浏览器环境**：`desktop` 为假时 `enterCompact/fitCompact/leaveCompact` 直接返回，`CompactChat` 走 `geometry` 预览分支；`getLocalServiceStatus` 不会被调用。
9. **退出时仍在启动**：`launch()` 在关键步骤检查 `closing`，必要时启动后立即 `stop()`。

## 输入输出示例

**示例 1（R1，状态响应）**

```json
{ "status": "connected", "base_url": "http://127.0.0.1:8765", "managed": true, "error": null }
```

前端据此 `switchMode('service', base_url)`；`managed: true` 表示退出程序时会清理该服务。

**示例 2（R4，展开时适配）**

输入：`height = 280 + 116 + 16 = 412`（消息区 280、chrome 116）、`minimumHeight = 72 + 116 + 16 = 204`；当前窗口 `size = { width: 1013, height: 126 }`（150% 缩放）、`position = { x: 500, y: 700 }`、`scale = 1.5`。

计算：`nextHeight = round(min(412 * 1.5, 工作区高)) = 618`；`y = 700 + 126 - 618 = 208`，再钳制到工作区；调用 `set_compact_bounds` 一次完成移动与缩放。

**示例 3（R2，端口占用）**

`GET /api/health` 无响应，但 `TcpStream::connect_timeout` 成功：

```json
{ "status": "failed", "base_url": "http://127.0.0.1:8765", "managed": false, "error": "本机服务端口已被其他程序占用。" }
```

## 关联文档与验证依据

- 上级：[frontend/README.md](../README.md)
- 同级：[app/README.md](../app/README.md)（初始化与切换）· [chat/README.md](../chat/README.md)（简化模式）· [settings/README.md](../settings/README.md)（连接与重试）· [api/README.md](../api/README.md)（HTTP/SSE 客户端）
- 服务端 owner token 与关闭接口：[server/README.md](../../server/README.md) · CLI 启动器同类逻辑：[cli/README.md](../../cli/README.md)
- Rust 单元测试（[service.rs](../../../frontend/src-tauri/src/service.rs)）：`only_reuses_mybot_health_response`（只复用 mybot 健康响应）、`manual_service_has_no_child_to_terminate`（手工服务无子进程可关）、`reused_service_disconnect_is_reported`（复用服务断开报告为 failed）
- 前端脚本：[package.json](../../../frontend/package.json) 的 `test:desktop`（`node scripts/verify-desktop.mjs`）、`test:service`（`node scripts/verify-service.mjs`）；桌面测试截图输出在 `test-results/desktop/`
- 历史验收记录（原 `frontend/README.md`，已迁移）：2026-09-21 本机 150% 缩放下通过简化模式尺寸/位置、失焦隐藏与后台未读、自动启动、服务复用、owner 保护和退出清理；2026-09-22 更新后的桌面程序通过正式对话、简化模式、历史恢复及正常退出测试，NSIS 安装包已重建。多显示器混合 DPI、原生鼠标拖拽和实际安装/卸载流程仍需人工验收。
- 未验证项：本页为静态阅读源码与既有测试整理，本次未重新执行 Rust/桌面测试；`src-tauri/target/` 构建产物未在本轮检查。
