# 偏好设置与连接配置（settings/）

## 职责与入口

`SettingsPage` 是“设置”一级导航页面，四个标签：`外观`、`立绘显示`、`简化模式`、`连接与语音`。同目录的 `ServiceSettingsPanel` 负责服务模式/演示模式切换、连接状态、模型节点编辑与保存/应用、语音说明。模块类型为 React 页面组件，不是 Agent 图节点；偏好字段定义见 [types.ts](../../../frontend/src/shared/types.ts)，服务端模型设置见 [server/README.md](../../server/README.md)。

- **源码**：[SettingsPage.tsx](../../../frontend/src/features/settings/SettingsPage.tsx)、[ServiceSettings.tsx](../../../frontend/src/features/settings/ServiceSettings.tsx)
- **入口**：`App` 在 `page === 'settings'` 时渲染 `<SettingsPage currentCharacter={thread?.characterId ?? ''} enterCompact={() => void toggleCompact(true)} />`，见 [app 叶子](../app/README.md) R4。
- **上游**：`App`、`WorkspaceStore`（`preferences`、`characters`、`mode`、`baseUrl`、`connection`、`connectionError`、`ready`）；**下游**：`workspaceStore.setPreferences/switchMode/refresh`、`HttpService.settings/models/saveModels/applyModels`、Tauri 命令 `local_service_status`/`start_local_service`。
- **触发时机**：进入页面、切换标签、修改偏好（即时保存）、连接/切换模式、读取/保存/应用模型设置。

## 调用链总览

```text
R1 SettingsPage 挂载：标签状态、当前角色立绘解析、桌面服务状态 2 秒轮询
R2 外观标签：update(patch) → workspaceStore.setPreferences → document.dataset（由 App 应用）
R3 立绘标签：updateSprite(patch) → preferences.sprites[角色]
R4 简化模式标签：replyPlacement/alwaysOnTop 等偏好 + enterCompact() → App.toggleCompact(true)
R5 连接标签：本机 API 状态与重试；ServiceSettingsPanel 模式切换与模型设置
R6 ServiceSettingsPanel 读取：api.settings() + api.models() → settings/models/nodes
R7 模型保存/应用：api.saveModels(saved_version, nodes) → api.applyModels(saved_version) → workspaceStore.refresh()
```

本模块没有工厂或构建阶段：两个导出组件都由父级渲染时实例化，状态用 `useState` 持有。

## 运行链

### R1. `SettingsPage` 挂载

- 定位与签名：`SettingsPage({ currentCharacter, enterCompact }: { currentCharacter: string; enterCompact: () => void })`。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `currentCharacter` | `string` | `App` 的活动线程角色 | 可为空串 | 立绘标签的初始角色与简化模式入口 |
| `enterCompact` | `() => void` | `App.toggleCompact(true)` | 必填 | “进入简化模式”按钮 |
| `preferences` | `Preferences` | `useWorkspace()` | zod 默认值 | 全部设置项 |
| `characters` | `Character[]` | `useWorkspace()` | 必填 | 立绘角色下拉与素材 |
| `desktop` | `boolean` | platform | 必填 | 是否显示本机 API 状态 |
| `tab` / `selected` | `string` | `useState('appearance')` / `useState(currentCharacter)` | — | 当前标签与立绘角色 |
| `service` | `LocalServiceStatus?` | `useState()` | — | 本机服务状态 |

桌面模式 `useEffect`：立即调用 `getLocalServiceStatus()`，随后每 2 秒刷新；读取失败时写入 `{ status: 'failed', base_url: '', managed: false, error: '无法读取本机服务状态。' }`。非桌面直接跳过。

### R2. 外观标签

| 设置项 | 控件 | 输入 | 写入 |
| --- | --- | --- | --- |
| 主题 | `<select>`：`system`/`dark`/`light` | 跟随系统 / 深蓝灰 / 浅色 | `update({ theme })` |
| 强调色 | 两个按钮：`violet`/`rose` | 柔紫 / 玫瑰 | `update({ accent })` |
| 阅读密度 | `<select>`：`comfortable`/`compact` | 舒适 / 紧凑 | `update({ density })` |
| 情境面板 | 复选框 | 默认展开世界、角色与记忆信息 | `update({ inspector })` |

`update(patch)` 调用 `workspaceStore.setPreferences(patch)`，经 `preferencesSchema` 校验后持久化；`App` 的 effect 把 `theme/accent/density` 写入 `document.documentElement.dataset`，CSS 用 `light-dark()` 与 `[data-accent]` 切换 token（见 [design 叶子](../design/README.md)）。

### R3. 立绘显示标签

- 角色下拉：`select` 写入本地 `selected`；`character = characters.find(id === selected) ?? characters[0]`。
- `sprite = preferences.sprites[character?.id ?? ''] ?? spriteSchema.parse({})`；`asset = character?.assets.find(item => item.id === sprite.assetId)`。
- `updateSprite(patch)`：`update({ sprites: { ...preferences.sprites, [character.id]: { ...sprite, ...patch } } })`。

| 设置项 | 控件与范围 | 输入字段 |
| --- | --- | --- |
| 工作台立绘 | 复选框，无素材时禁用 | `enabled` |
| 图片素材 | `<select>`：未选择 + 素材列表 | `assetId` |
| 位置 | `<select>`：靠右/靠左 · 底部对齐 | `side ∈ {right, left}` |
| 缩放 | `range` 50–150、步长 5，`<output>{scale}%</output>` | `scale` |
| 镜像 | 复选框 | `mirror` |

选择素材后若 `asset` 存在，渲染 `sprite-preview`：`height: scale * 2.2px`、`mirror` 时 `scaleX(-1)`，并显示素材名。提示“简化模式保持单个输入横条，不显示立绘。”。

### R4. 简化模式标签

| 设置项 | 控件 | 输入 | 说明 |
| --- | --- | --- | --- |
| 消息气泡位置 | `<select>`：`bubble`/`inline` | 输入条上方 / 输入条内上沿 | 对应 `CompactChat` 的渲染分支，见 [chat 叶子](../chat/README.md) R6 |
| 窗口尺寸 | 只读 `<output>` | 宽 `compactWidth` px · 消息区高 `historyHeight` px | 由拖拽边沿更新，不提供滑动条 |
| 窗口置顶 | 复选框 | `alwaysOnTop` | 浏览器提示“此偏好用于桌面版” |
| 失焦时 | 固定说明 | “隐藏消息与历史，保留输入草稿” | 不可关闭 |
| 输入格式 | 固定说明 | Enter 发送 · Shift+Enter 换行 | 保留换行、空行和缩进 |
| 进入简化模式 | 主按钮 | `currentCharacter` 非空才可用 | 调用 `enterCompact()` |

### R5. 连接与语音标签

| 区块 | 输入 | 行为 |
| --- | --- | --- |
| 本机 API（仅桌面） | `service` 状态 | 显示“已连接 · base_url”“连接失败”或“正在启动…”；`status === 'failed'` 时提供“重试启动服务” → `startLocalService().then(getLocalServiceStatus).then(setService)` |
| `ServiceSettingsPanel` | 见 R6/R7 | 数据来源、地址、模型设置 |
| 语音播放 | 固定说明 | “服务模式中可为正式角色回复生成语音，并在消息下方播放。” |

### R6. `ServiceSettingsPanel` 读取

- 定位与签名：`ServiceSettingsPanel()`；通过 `workspaceStore.service` 取适配器。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `mode` / `address` | `string` | `useState(workspace.mode / baseUrl || 'http://127.0.0.1:8765')` | — | 模式与地址 |
| `settings` | `SettingsStatus?` | `api.settings()` | — | 模型配置状态与凭据提示 |
| `models` | `ModelSettings?` | `api.models()` | — | 版本、待应用标记与节点 |
| `nodes` | `Record<string, NodeModel>` | `useState` | 从 `models.nodes` 复制 | 编辑中的节点 |
| `dirty` | `boolean` | `JSON.stringify(nodes) !== JSON.stringify(models.nodes)` | — | 保存按钮可用性 |

`useEffect(..., [api])` 调用 `readModels()`：并行 `api.settings()` 与 `api.models()`，只有 `workspaceStore.service === api` 时才写入状态，避免切换服务后旧响应覆盖。演示模式直接返回。

### R7. 模型节点编辑、保存与应用

| 交互 | 调用 | 条件 | 输出 |
| --- | --- | --- | --- |
| 保存模型设置 | `api.saveModels(models.saved_version, nodes)` | `dirty` 且所有节点 `model.trim()` 非空 | 更新 `models/nodes`，提示“已保存到本机。点击‘应用已保存设置’后供下一任务使用。” |
| 应用已保存设置 | `api.applyModels(models.saved_version)` → `workspaceStore.refresh()` | `!dirty && models.pending_changes` | 提示“已应用，下一个任务使用新设置。” |
| 放弃编辑并重读 | `readModels()` | 无 | 丢弃本地编辑 |

节点网格只渲染 `labels` 中存在的 6 个节点：`main`（角色回复）、`participant_state`（角色与用户状态）、`memory_query`（记忆查询）、`memory_summary`（记忆整理）、`chunking`（记忆分块）、`tts`（语音指令）。每个 `fieldset` 的字段：

| 字段 | 控件 | 取值 |
| --- | --- | --- |
| 服务商 | `<select>` | `deepseek` / `moonshot` / `llama_cpp`（显示“本地模型”） |
| 模型 | `<input maxLength=200>` | 任意非空字符串 |
| 思考模式 | `<select>` | `enabled` / `disabled`（开启/关闭） |
| 思考强度 | `<select>` | `none/low/medium/high/max`（无/低/中/高/最高） |

节点下方依据 `settings.models[name]` 显示提示：`credentials_configured === false` → “尚未配置凭据”；`local_model_available === false` → “本地模型文件缺失”；否则“凭据和路径在本机配置”。

模式与地址区：`数据来源` 下拉（本机服务/演示模式）+ 服务地址输入 + 连接按钮，点击调用 `workspaceStore.switchMode(mode, address)`；连接状态显示 `connection` 与 `connectionError`，数据库显示 `ready.database`，对话与语音显示 `ready.capabilities.chat`。

## 分支与异常链

1. **浏览器环境**：不显示本机 API 轮询与状态；简化模式置顶偏好注明仅桌面生效。
2. **本机服务失败**：显示“连接失败”与错误，提供“重试启动服务”；失败再写入错误提示。
3. **服务地址非法**：`switchMode` 内 `localBaseUrl` 抛错，`error` 显示提示且不切换。
4. **模型版本冲突**：`saveModels`/`applyModels` 失败时保留编辑并显示服务端消息；`dirty` 时“应用”按钮禁用。
5. **节点模型为空**：保存按钮禁用，避免提交空模型名。
6. **切换服务后的旧响应**：`readModels` 用 `workspaceStore.service === api` 守卫丢弃。
7. **无角色素材**：立绘开关与素材下拉禁用；提示“此角色没有图片资源。”。
8. **`currentCharacter` 为空**：简化模式入口禁用。

## 输入输出示例

**示例 1（R2，偏好增量）**

点击“玫瑰”强调色：

```json
// workspaceStore.setPreferences({ accent: 'rose' }) 后持久化的 preferences 增量
{ "accent": "rose" }
```

`App` 把 `document.documentElement.dataset.accent` 设为 `rose`，CSS 规则 `:root[data-accent='rose']` 覆盖 `--accent`/`--tint`。

**示例 2（R5，切换模式）**

输入：`mode = "service"`、`address = "http://127.0.0.1:8765"`。

输出：`workspaceStore.switchMode` 重建 `HttpService`、写入 `CONNECTION_KEY` 并 `initialize()`；面板随后显示连接状态、数据库与能力。

**示例 3（R7，保存模型设置）**

输入：`models.saved_version = "2026-09-20T10:00:00Z"`、`nodes.main = { provider: "deepseek", model: "deepseek-chat", thinking: "enabled", reasoning_effort: "medium" }`。

请求：`PUT /api/settings/models`，body `{ "expected_version": "2026-09-20T10:00:00Z", "nodes": { "main": { ... } } }`。

输出：返回新 `saved_version` 与 `pending_changes: true`；提示保存成功，等待“应用已保存设置”。

## 关联文档与验证依据

- 上级：[frontend/README.md](../README.md)
- 同级：[app/README.md](../app/README.md)（`setPreferences`/`switchMode`）· [platform/README.md](../platform/README.md)（本机服务状态与启动）· [chat/README.md](../chat/README.md)（简化模式与语音）· [characters/README.md](../characters/README.md)（素材来源）· [design/README.md](../design/README.md)（视觉 token 与简化模式规范）
- 服务端设置与模型生效：[server/README.md](../../server/README.md)（`ModelSettingsService`、能力报告）
- 端到端测试：[service.spec.ts](../../../frontend/e2e/service.spec.ts)（“profile conflict retains the draft and model settings save and apply independently”）、[workspace.spec.ts](../../../frontend/e2e/workspace.spec.ts)（偏好刷新恢复、窄屏可用性）
- 历史验收记录（原 `frontend/README.md`，已迁移）：2026-09-21 覆盖模型保存与生效分离、主题/强调色/密度偏好；2026-09-22 覆盖模式切换及刷新后的历史一致。模型使用受控测试实现。
- 未验证项：本页为静态阅读源码与既有测试整理，本次未重新执行测试；密钥字段只返回“已配置/未配置”，本页未验证真实凭据流程。
