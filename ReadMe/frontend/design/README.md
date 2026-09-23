# 界面设计决定（design/）

## 职责与入口

本页是前端的设计参考，不是源码模块，也不存在运行时调用链。它由项目原设计文档（2026-09-13 修订 05，首版已确认）中仍然有效的内容整理而来，并标注每条决定的当前状态。阅读方式应是“设计约束 → 实现位置”，而不是“函数 A 调用函数 B”；实际的组件与函数链见 [app 叶子](../app/README.md)、[chat 叶子](../chat/README.md)、[settings 叶子](../settings/README.md)、[platform 叶子](../platform/README.md)。

| 状态标记 | 含义 |
| --- | --- |
| 已确认并实现 | 当前源码中存在对应实现，本页给出源码依据 |
| 已确认未实现 | 首版方案确认但当前源码未实现，保留为后续计划 |
| 历史记录 | 方案当时的背景或结论，仅用于解释取舍，不约束当前实现 |

## 调用链总览（不适用）

本页没有构建阶段与运行阶段。设计条目与实现位置的对应关系如下（不是调用关系）：

| 设计条目 | 实现位置 |
| --- | --- |
| 一级导航、外壳、专注阅读 | [App.tsx](../../../frontend/src/app/App.tsx)（`Page` 联合类型、`focus` 状态） |
| 工作台消息、输入、情境面板 | [features/chat/](../../../frontend/src/features/chat/)（见 [chat 叶子](../chat/README.md)） |
| 简化模式焦点组与几何 | [features/compact-chat/](../../../frontend/src/features/compact-chat/) |
| 视觉 token | [theme.css](../../../frontend/src/shared/theme.css) |
| 偏好字段与默认值 | [types.ts](../../../frontend/src/shared/types.ts) 的 `preferencesSchema` |
| 窗口与置顶 | [src-tauri/src/](../../../frontend/src-tauri/src/)（见 [platform 叶子](../platform/README.md)） |

## 布局方向（已确认并实现）

方案比较过三个方向：

| 方向 | 结论 | 当前状态 |
| --- | --- | --- |
| A. 对话工作台 | 首版默认：历史易读、状态与记忆随时可查 | 已确认并实现；`App` 默认 `page = 'chat'` |
| B. Galgame 舞台 | 后续作为同一会话的显示模式 | 已确认未实现；当前只有工作台与简化模式 |
| C. 悬浮简化模式 | 桌面壳提前交付，只保留圆角输入横条 | 已确认并实现；见 `CompactChat` |

“专注阅读”和“简化模式”是两个不同能力：前者只收起工作台左右栏（`App.focus` 与 CSS `.focus-reading`），不改变窗口；后者退出工作台布局，由 Tauri 把窗口缩成无边框置顶输入条。两者都不新建会话、不改变模型参数、不重发输入。

## 参考项目映射（历史记录）

原方案参考 `D:/Coding Tools/Codes/资源逆向/Shinsekai`，映射关系与当前状态：

| 参考依据 | 已确认内容 | 对 mybot 的影响 | 当前状态 |
| --- | --- | --- | --- |
| `design.md`、`theme/tokens.css` | 设置页分组、舞台分层、token 管理颜色 | 建立独立 token 与组件 | 已确认并实现（[theme.css](../../../frontend/src/shared/theme.css)） |
| `AppShell.tsx`、`SidebarNav.tsx` | 配置与工具入口按功能组织 | 一级导航收敛为对话、角色、记忆、设置、会话管理 | 已确认并实现（`navigation` 数组） |
| `ChatStagePage.tsx`、`state/types.ts` | 演出层、输入、历史、音频独立状态 | 对话数据与显示模式分离 | 部分实现：数据与显示分离；舞台未实现 |
| mybot `main.py`、`agent/builder.py` | CLI 使用 thread_id 与 Postgres checkpoint；草稿/检查/提交流程 | 新增服务与 Web API，只展示正式提交正文 | 已确认并实现（[server/README.md](../../server/README.md)） |
| `agent/classes/state.py` 等 | 世界日期/星期/时段/天气与双方位置、情绪、身体、穿着；角色另有听觉 | 状态栏只显示真实字段，不造好感度/关系数值 | 已确认并实现（`StateBlock`、`stateDto`） |
| `agent/memory/store.py`、`node/memory.py` | 按角色存储长期记忆、增改删与混合检索 | 记忆浏览复用服务，聊天记录独立持久化 | 已确认并实现（只读浏览） |
| `agent/utils/character.py`、`Character/` | Markdown 角色档案 | 兼容现有目录，不预设 `.char` | 已确认并实现 |
| `agent/node/tts.py` | 本地 Qwen TTS 由 sounddevice 播放，无音频 URL | 拆分合成、资源与播放器 | 已确认并实现（`speech_jobs` + `<audio>`） |
| `config/models.yaml`、`model_config.py` | 节点模型配置合并与缓存 | 设置保存后明确生效时机 | 已确认并实现（保存/应用分离） |

## 视觉规范（已确认并实现）

深浅主题通过同一组语义 token 切换，默认跟随系统；实际取值来自 [theme.css](../../../frontend/src/shared/theme.css)：

| token | 浅色 | 深色 | 用途 |
| --- | --- | --- | --- |
| `--bg` | `#f5f5f8` | `#282c34` | 应用背景 |
| `--nav` | `#ebecf2` | `#21252b` | 标题栏与侧栏 |
| `--panel` | `#fff` | `#2c313a` | 面板、用户气泡 |
| `--input` | `#fff` | `#1b1d23` | 输入框、助手气泡 |
| `--line` | `#dadde6` | `#414755` | 分隔线 |
| `--text` | `#292838` | `#e8e7ee` | 正文 |
| `--muted` | `#666878` | `#a9afc0` | 次要文字 |
| `--accent`（柔紫） | `#7950ad` | `#bd93f9` | 强调色 |
| `--tint` | `#ebe3f6` | `#393147` | 选中/悬停底色 |
| `--on-accent` | `#fff` | `#241a33` | 强调色上的文字 |
| `--good` / `--danger` | `#26705b` / `#a42f4e` | `#8fd0b6` / `#ffb0bc` | 成功/危险 |

- 玫瑰强调色由 `:root[data-accent='rose']` 覆盖：浅色 `#ab426e`、深色 `#f3a4c6`（`--tint` 同步为 `#f5e1ea` / `#44303d`）。
- 主题由 `:root[data-theme='light'|'dark']` 与 CSS `light-dark()` 控制；`App` 把偏好写入 `document.documentElement.dataset`。
- 字体：`'Segoe UI', 'Microsoft YaHei', sans-serif`，基础 14px；对话正文 16px、行高 1.85；简化模式正文 14px、行高 1.8。
- 圆角：输入控件 7px，用户气泡 12px，简化模式输入条 30px，主要浮层（对话框）14px。
- 图标使用 Lucide；焦点可见（`:focus-visible` 2px 强调色描边）；`prefers-reduced-motion` 下关闭旋转动画；粗指针设备放大点击区域与字号。
- 响应式断点（实现值）：`1179px` 以下情境面板改为覆盖式抽屉，`899px` 以下侧栏改为抽屉加遮罩，`639px` 以下压缩工具栏与消息字号并隐藏工作台立绘。原方案写的是 1180/900/640，属于同一断点的四舍五入表述。

已知差异（静态核对，未运行时验证）：部分后加组件样式（`.service-notice`、`.pending-input`、`.memory-policy`、`.managed-row` 等）引用 `var(--surface)` 与 `var(--border)`，而当前 `:root` 只定义了 `--panel` 与 `--line`，这两个变量在 `theme.css` 中未定义。

## 对话工作台规范（已确认并实现）

- 三栏结构：会话侧栏 208px / 对话正文自适应 / 情境面板 272px。原方案写 288px，实现为 272px（`.inspector`）；情境面板在窄屏变为抽屉。
- 侧栏含产品导航、新建对话、最近会话与底部连接说明；会话项显示标题、角色名、未读点与“处理中/本地预览”状态。
- 新建会话先选角色、填可选标题、设置两项记忆开关，再创建 `thread_id`；会话绑定角色，切换角色应新建会话。
- 输入：Enter 发送、Shift+Enter 换行，中文输入法组合期间不发送；发送中禁用重复提交并保留可编辑的下一条草稿。
- 字段缺失统一显示“未记录”，不填推测值；世界时间与消息发送时间分别展示。
- 情境面板的“本轮记忆”只表示检索工具返回了这些记录，不声称模型一定使用或引用；服务模式无命中时显示“暂无本轮检索记录。”，演示模式只在内置示例会话显示虚构示例。
- 回复检查始终由后端执行，前端不提供检查专属开关；状态栏按真实 `phase` 显示“正在准备/正在回复/整理回复/正在检索记忆/正在使用工具/正在更新状态/正在提交记忆任务”。

## 简化模式交互规范（已确认并实现）

进入条件：已选定角色和会话；从对话页点击“简化模式”。模式切换共用同一 `thread_id`、运行状态、消息与草稿，不新建会话、不重新发送。

| 规范条目 | 决定内容 | 实现与差异 |
| --- | --- | --- |
| 空闲外观 | 宽约 560–680px、高约 58–64px 的圆角横条，保留角色标识、输入、发送、返回工作台；桌面版另有“退出程序” | 默认宽 660px（偏好 `compactWidth`，范围 320–2400）；输入条 `.compact-composer` 最小高 62px；初始窗口高 84×缩放 |
| 聚焦展开 | 只有文本输入框被点击或获得键盘焦点才展开消息；拖动把手、返回按钮、角色标识、窗口重新激活均不展开 | 由 `onFocusCapture`/`onPointerDown` 命中 textarea 实现；见 [chat 叶子](../chat/README.md) R6 |
| 交错气泡 | 按真实时间顺序，角色靠左、用户靠右；每条一个气泡，垂直间隔约 18px；宽度随文字变化 | CSS `.compact-group .message` 与 `gap: 18px`；最大宽度 86%，639px 以下 92% |
| 回复位置 A（默认） | 用户与角色气泡都在横条上方的可滚动消息区 | 偏好 `replyPlacement: 'bubble'` |
| 回复位置 B | 同一份交错气泡流显示在输入条内部上沿 | 偏好 `replyPlacement: 'inline'` |
| 输入条增高 | 聚焦时随草稿增高，最多约 120px，内部滚动；底部位置保持不动 | `Composer` 在 `compact` 下 36–120px；原生窗口同步调整 |
| 边沿调节宽高 | 左右调宽、上下调高、四角双向；对边固定；失焦仅保留左右调宽 | 展开渲染 8 个方向，收起只渲染 `e/w`；浏览器预览另有 8px 方向键步进 |
| 尺寸约束 | 默认宽 660、消息区高 280；最小宽 320；消息区 72–560，且受可用空间限制 | 偏好钳制为宽 320–2400、消息区 72–1600；原生模式再由 `fitCompact` 按工作区限制；最小消息区 72px |
| 滚动阅读 | 只有消息区纵向滚动，气泡不截断、不单独滚动；滚轮/触控板/滚动条可用 | `MessageList` 的 `.message-list` 负责滚动 |
| 阅读位置 | 距底部约 24px 内自动跟随；上翻保持位置并显示“有新消息 · 回到最新”；主动发送或点击后回到底部 | 判定阈值 24px；`ReadingPosition` 含 `anchorId/offset`，分页追加时保持锚点 |
| 失焦规则 | 离开输入条与消息浮层这一焦点组、或切换应用时立即隐藏消息，保留草稿；内部发送、复制、拖动不误收起 | `pointerdown` 外部判定 + `window.blur` + `visibilitychange` + Tauri 焦点事件 |
| 后台回复到达 | 继续保存，只显示“有新回复”；不弹正文、不抢焦点 | `thread.unread` + 收起状态的 `.unread-label` |
| Escape | 收起消息并使输入框失焦，再次点击输入框恢复 | `onKeyDown` 的 `Escape` 分支 |
| 生成中 | 发送入口忙碌且不可重复发送；可切换模式或失焦，运行继续 | `Composer` 的 `busy` 与 store 的 `pending` |
| 拖动 | 横条内小型拖动把手，记住屏幕内位置；布局变化后钳制到可见区域 | `dragNative` / 浏览器指针拖拽；`compactAnchor` 保存底部位置 |
| 立绘 | 简化模式默认不显示立绘，工作台开关不自动带入 | 实现一致：简化模式不渲染立绘 |

## 分阶段构建计划（历史计划与状态）

原方案的分阶段交付与当前状态：

| 阶段 | 原交付 | 当前状态 |
| --- | --- | --- |
| 0. 方案评估 | 第一版外观基线、原文记忆列表、立绘设置、交错气泡、多行输入、边沿拖拽与滚动 | 已确认并实现 |
| 1. 前端与桌面骨架 | React 页面、基础主题、模拟 API、Tauri 工作台/浮窗切换 | 已确认并实现 |
| 2. 真实文字对话闭环 | Python 服务、完整历史、会话恢复、检查后正文、状态面板、原文记忆、基本立绘、共享会话的简化模式 | 已确认并实现 |
| 3. 管理与声音 | 档案编辑、记忆维护、配置保存/生效、音频服务与回放 | 部分实现：档案编辑、配置保存/生效、音频回放已实现；记忆编辑/删除未实现（当前只读） |
| 4. 完整演出 | 表情立绘映射、背景、对白分段、语音同步 | 已确认未实现 |

非目标（首版不纳入，当前仍未发现实现）：插件市场、MCP 管理、ASR、文生图、复杂世界书、好感度系统、多角色同场、主题编辑器。

## 输入输出示例（设计取值 → 界面效果）

**示例 1（默认偏好）**

`preferencesSchema.parse({})` 的结果：

```json
{ "theme": "system", "accent": "violet", "density": "comfortable", "inspector": true,
  "replyPlacement": "bubble", "alwaysOnTop": true, "compactWidth": 660, "historyHeight": 280, "sprites": {} }
```

对应界面：跟随系统配色、柔紫强调、舒适密度、情境面板默认展开、简化模式气泡在输入条上方、窗口置顶、宽 660、消息区 280。

**示例 2（立绘偏好）**

```json
{ "sprites": { "SuLi": { "enabled": true, "assetId": "suli-2", "side": "left", "scale": 120, "mirror": true } } }
```

对应界面：工作台立绘靠左、底部对齐、高为容器 120%、水平翻转；简化模式不显示。

**示例 3（简化模式交互状态）**

```text
收起：仅输入条（+ 未读提示）
聚焦输入框：消息区向上展开，输入条底边不动
Escape：消息区收起，输入框失焦
后台回复：仍收起，显示“有新回复”
```

## 关联文档与验证依据

- 上级：[frontend/README.md](../README.md)
- 同级：[chat/README.md](../chat/README.md)（简化模式组件链）· [settings/README.md](../settings/README.md)（偏好字段）· [app/README.md](../app/README.md)（外壳与主题应用）· [platform/README.md](../platform/README.md)（窗口实现）
- 源码依据：[theme.css](../../../frontend/src/shared/theme.css)、[types.ts](../../../frontend/src/shared/types.ts)、[CompactChat.tsx](../../../frontend/src/features/compact-chat/CompactChat.tsx)、[geometry.ts](../../../frontend/src/features/compact-chat/geometry.ts)
- 端到端验证：[workspace.spec.ts](../../../frontend/e2e/workspace.spec.ts)（简化模式缩放、最小尺寸、仅输入框展开、后台回复隐藏、窄屏可用性）
- 历史记录：原设计文档（2026-09-13 修订 05，首版已确认）的仍然有效内容已全部并入本页，原文件已移除；当时“模型、数据库和语音尚未接入”的状态已被后续阶段取代。
- 未验证项：本页数值为静态阅读源码整理，本次未在真实桌面环境复测；`--surface`/`--border` 未定义的影响未做运行时验证；阶段 3 的“记忆维护”与阶段 4 的“完整演出”均未实现。
