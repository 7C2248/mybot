# 前端（React + Tauri）

`frontend/` 是 mybot 的桌面与浏览器界面：React 19 + TypeScript + Vite 7 + Tauri 2。桌面程序默认自动启动或复用本机 Python API 并使用真实会话；浏览器默认演示模式，两种模式可在“设置 → 连接与语音”切换，数据相互独立。

本页是前端子系统总览，并保留启动构建、桌面打包与验收记录；组件级调用链下沉到各叶子文档。

## 子目录与节点

| 名称 | 类型 | 主要功能 | 协作对象 | 源码 | 详细文档 |
| --- | --- | --- | --- | --- | --- |
| `app/` | 应用外壳与状态 | `App.tsx` 导航与外壳；`WorkspaceStore` 状态、持久化、轮询与恢复 | 全部 features、api、platform | [App.tsx](../../frontend/src/app/App.tsx)、[store.ts](../../frontend/src/app/store.ts) | [app/README.md](app/README.md) |
| `api/` | 服务适配 | `HttpService`（HTTP/SSE/Zod 校验）、DTO 协议、演示适配器 | app、全部 features | [http.ts](../../frontend/src/shared/api/http.ts)、[contracts.ts](../../frontend/src/shared/api/contracts.ts)、[demo.ts](../../frontend/src/shared/api/demo.ts) | [api/README.md](api/README.md) |
| `chat/` | 对话与简化模式 | 工作台对话、输入、消息列表、语音播放、情境面板；悬浮输入条与几何计算 | app、api、platform | [features/chat/](../../frontend/src/features/chat/)、[features/compact-chat/](../../frontend/src/features/compact-chat/) | [chat/README.md](chat/README.md) |
| `characters/` | 角色页 | 档案按语言编辑与版本冲突处理、立绘素材选择 | app、api、server/characters | [CharactersPage.tsx](../../frontend/src/features/characters/CharactersPage.tsx) | [characters/README.md](characters/README.md) |
| `memory/` | 记忆页 | 原文搜索、游标分页与详情 | app、api、server/memory | [MemoryPage.tsx](../../frontend/src/features/memory/MemoryPage.tsx) | [memory/README.md](memory/README.md) |
| `conversations/` | 会话管理 | 列表/回收站/CLI 历史、策略编辑、检查点重载 | app、api、server/conversations | [ConversationManager.tsx](../../frontend/src/features/conversations/ConversationManager.tsx) | [conversations/README.md](conversations/README.md) |
| `settings/` | 设置页 | 外观、立绘、简化模式、连接与模型设置 | app、api、platform | [SettingsPage.tsx](../../frontend/src/features/settings/SettingsPage.tsx)、[ServiceSettings.tsx](../../frontend/src/features/settings/ServiceSettings.tsx) | [settings/README.md](settings/README.md) |
| `platform/` | 桌面能力 | 服务探测/启动/复用/退出；简化模式窗口、拖动与边沿缩放；Tauri 命令 | app、chat、settings | [service.ts](../../frontend/src/shared/platform/service.ts)、[window.ts](../../frontend/src/shared/platform/window.ts)、[src-tauri/src/](../../frontend/src-tauri/src/) | [platform/README.md](platform/README.md) |
| `design/` | 设计参考 | 布局方向、视觉 token、简化模式交互规范、阶段计划与非目标（非运行调用链） | chat、settings、platform | 见文档 | [design/README.md](design/README.md) |

## 页面与导航

一级导航（无路由库，`Page` 联合类型切换）：对话、角色、记忆、设置、会话管理。对话页检查器分 `状态`（世界/角色/用户）与 `记忆`（本轮检索命中）两个标签；模型设置节点为 `main / participant_state / memory_query / memory_summary / chunking / tts`。详细结构见 [app/README.md](app/README.md)。

## 整体数据流

- **服务模式**（桌面默认）：`HttpService` 连接回环 Python 服务；5 秒轮询健康/会话/旧历史，运行期间轮询快照并消费 SSE；本机按服务地址隔离缓存草稿、阅读位置、未读、pending 请求与偏好。
- **演示模式**（浏览器默认）：`demoService` 从 `/local-characters/manifest.json` 读取本地角色资源，模拟 phase 与固定回复，不访问数据库或模型。
- 写操作携带 `expected_version`/`client_request_id`；不确定失败保留 pending 输入并用同一请求键确认；版本回退的轮询结果被忽略。细节见 [api/README.md](api/README.md) 与 [app/README.md](app/README.md)。

## 启动与构建

需要 Node.js 22.12 或更高版本。在本目录运行：

```powershell
npm ci
npm run dev        # http://127.0.0.1:5173
npm run build      # 类型检查并构建到 dist/
npm run preview    # 在 127.0.0.1:4173 查看构建结果
npm test                  # 单元测试
npm run test:e2e           # 演示浏览器流程（默认 Microsoft Edge）
npm run test:service-ui    # 独立测试库中的实际 API 浏览器流程，模型受控
```

- 执行 `npm ci` 前先停止正在运行的开发服务器，避免 Windows 锁定 esbuild 文件。没有 Edge 时可设置 `PLAYWRIGHT_CHANNEL=chrome`。
- `npm test` 和普通 `test:e2e` 不连接数据库或真实模型；`test:service-ui` 需要已配置且有建库权限的 `DB_URL`，自动创建和清理独立数据库及临时资源。真实模型验收：`node scripts/test-service-ui.mjs --desktop --live`。
- `npm run assets`（由 `predev`/`prebuild`/`pretest:e2e` 自动执行）从 `../Character/<角色>/` 生成 `public/local-characters/manifest.json` 与图片副本；缺少角色目录时仍可运行空资源前端。生成目录与 `dist/` 已忽略 Git。
- `package-lock.json` 固定依赖。当前 Windows 环境 npm 10 在更新 Vitest 可选依赖时曾出现内部解析错误；可临时使用 `npm exec --yes --package=npm@11 -- npm install`，不替换系统 npm。

### 桌面构建

准备好 Rust/Cargo、Windows C++ 构建工具及 WebView2 后：

```powershell
npm run desktop:dev
npm run desktop:build
npm run test:desktop  # 验证刚构建的 Windows 程序
npm run test:service  # 独立端口、禁用推理的桌面服务生命周期实测
```

- Rust 安装在自定义目录且未加入 PATH 时，将 `.env.desktop.example` 复制为 `.env.desktop.local`，填写实际的 `CARGO_HOME` 与 `RUSTUP_HOME`；构建脚本读取该文件补充 PATH，本地环境文件不提交。
- `src-tauri/Cargo.lock` 固定 Rust 依赖。下载 NSIS 受限时使用项目缓存 `src-tauri/target/.tauri/NSIS/`（已启用 `bundle.useLocalToolsDir`）；已有可执行文件时可用 `node scripts/desktop.mjs bundle --bundles nsis` 重新打包，`node scripts/desktop.mjs build --no-bundle` 只编译程序。
- 本机可直接运行 `src-tauri/target/release/mybot-desktop.exe`；NSIS 安装包位于 `src-tauri/target/release/bundle/nsis/`。安装包尚未内置 Python、项目源码或模型权重，新机器需自行准备服务环境。

桌面连接与窗口行为见 [platform/README.md](platform/README.md)；服务端启动与托管退出见 [server/README.md](../server/README.md) 与 [server/http/README.md](../server/http/README.md)。

## 验证记录

以下为迁移自原前端说明与设计方案的验收记录，测试未在文档整理时重新执行：

- 2026-09-21：生产构建、16 项单元测试、9 项演示浏览器流程和 6 项实际 API 浏览器流程通过；打包桌面使用真实模型完成正式对话，验证模式切换及刷新后的历史一致；真实 TTS 权重合成尚未验收。桌面回归覆盖本机 150% 缩放下的两种回复布局、窗口宽高调整、72px 最小消息区、翻阅位置、跟随最新、多行输入收起、跨模式尺寸保存、工作台尺寸与位置恢复、失焦隐藏和后台未读提示。
- 2026-09-22：生产构建、19 项单元测试和 9 项实际 API 浏览器流程通过；确认发送回执与服务排队提示分开、后台记忆整理不阻塞下一轮回复、记忆状态读取失败不影响发送。更新后的桌面程序通过正式对话、简化模式、历史恢复及正常退出测试，NSIS 安装包重建。
- 未验证项：多显示器混合 DPI、原生鼠标拖拽、实际安装/卸载流程仍需人工验收；测试使用受控模型与记忆实现，未重复调用真实模型或加载真实 TTS 权重。

## 阅读导航

- 上级：[系统总览](../README.md) · 服务：[server/README.md](../server/README.md) · CLI：[cli/README.md](../cli/README.md)
- 叶子：[app](app/README.md) · [api](api/README.md) · [chat](chat/README.md) · [characters](characters/README.md) · [memory](memory/README.md) · [conversations](conversations/README.md) · [settings](settings/README.md) · [platform](platform/README.md) · [design](design/README.md)
- 前后端协议：[frontend/API_CONTRACT.md](../../frontend/API_CONTRACT.md) · 面向使用的项目入口：[README.md](../README.md)
