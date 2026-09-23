# 会话管理、回收站与 CLI 历史（conversations/）

## 职责与入口

`ConversationManager` 是“会话管理”一级导航页面，包含三个标签：`会话列表`（编辑标题与记忆策略、移入回收站）、`回收站`（恢复、永久删除）、`CLI 历史`（旧检查点关联、预览、导入）。页面同时导出 `MemoryPolicyFields`（新建对话与会话编辑器共用）和内部组件 `ConversationEditor`、`LegacyRow`。模块类型为 React 页面组件，不是 Agent 图节点；服务端行为见 [server/README.md](../../server/README.md)。

- **源码**：[ConversationManager.tsx](../../../frontend/src/features/conversations/ConversationManager.tsx)
- **入口 1**：`App` 在 `page === 'conversations'` 时渲染 `<ConversationManager key={\`${managedId}-${managedTab}\`} initialId={managedId} initialTab={managedTab} />`，见 [app 叶子](../app/README.md) R4。
- **入口 2（深链）**：最近会话右侧菜单设置 `managedId`/`managedTab='active'`；CLI 历史提示条设置 `managedTab='legacy'`。
- **上游**：`App`、`WorkspaceStore`（`threads`、`characters`、`legacy`、`mode`）；**下游**：`HttpService.threads/thread/updateThread/deleteThread/restoreThread/purgeThread/legacyThreads/legacyPreview/importLegacy`、`workspaceStore.manageThread/updateConversation/previewCheckpointSync/applyCheckpointSync/refresh`。
- **触发时机**：导航进入、切换标签、翻页、保存、删除/恢复/永久删除、预览或确认检查点同步、旧 CLI 轮询。

## 调用链总览

```text
R1 ConversationManager 挂载：按 initialTab 选标签 → load(false)
R2 标签切换/刷新：load(more=false|true) → api.threads / api.legacyThreads
R3 会话编辑（ConversationEditor）：reload / save / 检查点预览与确认
R4 回收站操作：change(thread, 'delete'|'restore'|'purge') → workspaceStore.manageThread → load
R5 CLI 历史（LegacyRow）：预览 → 关联并导入 → changed() → load + workspaceStore.refresh
R6 旧 CLI 轮询：标签为 legacy 且无游标、不忙时每 2 秒刷新一次
```

本模块没有工厂或构建阶段：所有组件由 `App` 渲染时实例化；`MemoryPolicyFields` 是无状态受控组件。

## 运行链

### R1. `ConversationManager` 挂载

- 定位与签名：`ConversationManager({ initialId, initialTab = 'active' }: { initialId?: string; initialTab?: 'active' | 'legacy' })`。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `initialId` | `string?` | `App.managedId` | 可选 | 直接打开该会话的编辑器 |
| `initialTab` | `'active' \| 'legacy'` | `App.managedTab` | 默认 `'active'` | 初始标签 |
| `workspace.mode` | `'demo' \| 'service'` | `useWorkspace()` | 必填 | 演示模式直接显示 `Empty` |
| `characters` | `Character[]` | `useWorkspace()` | 必填 | CLI 历史关联角色 |
| `tab` | `'active' \| 'trash' \| 'legacy'` | `useState(initialTab)` | — | 当前标签 |
| `items` / `legacy` | `Thread[]` / `LegacyThread[]` | `useState([])` | — | 列表数据 |
| `cursor` | `string \| null` | `useState()` | — | 分页游标 |
| `selected` / `purging` | `Thread?` | `useState()` | — | 编辑器目标与永久删除弹窗 |
| `busy` / `error` / `notice` | `boolean` / `string` | `useState` | — | 操作状态与提示 |

功能与内部调用：
1. `useEffect(..., [api, tab])`：清空 `selected/cursor/items/legacy` 后 `load()`；卸载时 `request.current++` 使在途响应作废。
2. `useEffect(..., [api, initialId])`：`initialId` 存在且服务模式时 `api.thread(initialId)` 并 `setSelected`，失败写 `error`。
3. `useEffect(..., [tab, busy, cursor])`：标签为 `legacy` 时建立 2 秒定时器，`!busy && !cursor` 才 `load()`；离开标签时清除。
4. 演示模式渲染 `Empty`：“会话管理用于本机服务 / 请在连接设置中切换到本机服务。”。

### R2. `load(more = false)`

- 定位与签名：组件内异步函数；用 `request.current` 自增的 `ticket` 丢弃过期响应。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `more` | `boolean` | 翻页按钮 | 默认 `false` | `true` 时带 `cursor` 追加 |
| `tab` | `'active' \| 'trash' \| 'legacy'` | R1 | 必填 | 数据源分支 |
| `cursor` | `string \| null` | 状态 | `more` 时使用 | 分页 |

功能与内部调用：
1. `setBusy(true)`、`setError('')`。
2. `legacy`：`api.legacyThreads(more ? cursor ?? undefined : undefined)` → 追加或覆盖 `legacy`。
3. 其余：`api.threads(more ? cursor ?? undefined : undefined, undefined, tab === 'trash')` → 追加或覆盖 `items`。
4. 每个分支记录 `setCursor(page.next_cursor)`；`finally` 仅在 ticket 未过期时 `setBusy(false)`。

输出：`items` 或 `legacy` 与 `cursor`；异常写入 `error`。

### R3. `ConversationEditor`（会话设置）

- 定位与签名：`ConversationEditor({ initial, saved }: { initial: Thread; saved: () => void })`；`key={selected.id}` 保证切换会话时重建。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `initial` | `Thread` | `ConversationManager.selected` | 必填 | 初始标题、策略、版本 |
| `saved` | `() => void` | 父组件闭包 | 必填 | 保存/同步后刷新列表 |
| `title` | `string` | `useState(initial.title)` | `maxLength = 200` | 标题输入 |
| `policy` | `MemoryPolicy` | `useState(initial.memoryPolicy ?? { retrieval: false, storage: false })` | — | 记忆策略 |
| `current` | `Thread` | `threads.find(id) ?? thread` | 必填 | 从快照取最新运行状态 |
| `active` | `boolean` | `!!current.pending \|\| (!!current.run && !terminal(current.run))` | — | 执行中禁止检查点操作 |

方法链：

| 方法 | 调用 | 输入 | 行为与输出 |
| --- | --- | --- | --- |
| `reload()` | `api.thread(thread.id)` | 服务模式 | 重读标题/策略并提示“已读取最新设置。” |
| `save()` | `workspaceStore.updateConversation(thread.id, thread.version!, title, policy)` | 标题非空 | 成功更新本地 `thread` 并提示“已保存，新的输入使用此记忆策略。”；失败显示服务端错误（版本冲突保留编辑） |
| `previewSync()` | `workspaceStore.previewCheckpointSync(thread.id)` | `!busy && !syncBusy && !active` | 打开弹窗，先 dry-run 拉取差异；失败写 `syncError` |
| `applySync()` | `workspaceStore.applyCheckpointSync(thread.id, sync)` | 有预览且 `delete_count > 0` | 确认截断；成功提示“已按检查点截断 N 条消息。”；409 时提示重新预览 |

界面要点：
- `MemoryPolicyFields`：两个复选框“检索长期记忆”“将本会话内容存入长期记忆”，提示“关闭后仍保存聊天历史。重新开启存储只处理之后的新输入；只存不读时仅新增记忆。”。
- 保存区提示“关闭存储会撤销尚未完成的记忆任务，已经写入的角色记忆保留。”；按钮“保存会话设置”“放弃编辑并重读”。
- 检查点区提示“读取最新检查点并与当前历史比对；检查点中不存在的较新消息会被永久删除…已经写入的长期记忆不会回滚。”；执行中显示“会话仍在执行，结束后才能重新加载。”。
- 确认弹窗展示 `sync.latest_message_text`、`delete_count`、`run_count`、`delete_preview`（最多 20 条，`preview_truncated` 时提示）、语音资源排队清理说明；按钮“取消/重新预览/确认截断”。

### R4. 删除、恢复与永久删除

| 步骤 | 调用 | 输入 | 输出与副作用 |
| --- | --- | --- | --- |
| 1 | `change(thread, action)` | `action ∈ {'delete','restore','purge'}` | `setBusy(true)`、清提示 |
| 2 | `workspaceStore.manageThread(thread.id, thread.version!, action)` | 版本必填 | 调对应端点；非恢复动作在 store 内 `forgetThread` 并 `refresh()` |
| 3 | `setSelected(undefined)`、`setPurging(undefined)` | — | 关闭编辑器/弹窗 |
| 4 | `setNotice(...)`、`await load()` | 文案按动作区分 | “会话已移入回收站。角色长期记忆保留。”/“会话已恢复。”/“会话已永久删除。” |

永久删除前渲染确认 `Dialog`：“永久删除‘{title}’及其聊天历史、运行记录和音频？此操作无法恢复。”。

### R5. `LegacyRow`（CLI 历史）

- 定位与签名：`LegacyRow({ item, changed }: { item: LegacyThread; changed: () => void })`。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `item` | `LegacyThread` | `ConversationManager.legacy` | 必填 | 来源 id、状态、标题、已知角色 |
| `character` | `string` | `useState(item.character_id ?? '')` | — | 关联角色 |
| `running` | `boolean` | `item.status ∈ {queued, running}` | — | 导入中禁用 |
| `preview` | `{ items, notice }?` | `useState` | — | 预览弹窗 |

| 交互 | 调用 | 条件 | 输出 |
| --- | --- | --- | --- |
| 选择角色 | `setCharacter` | `!item.character_id` | 已确定角色的条目禁止修改 |
| 预览历史 | `api.legacyPreview(item.source_id)` | 非忙 | 弹窗展示 `notice` 与消息正文（user 标“你”，其余标“角色”） |
| 关联并导入 | `api.importLegacy(item.source_id, character, item.title ?? 'CLI 历史')` → `changed()` | 角色非空、非 running | 父组件 `load()` + `workspaceStore.refresh()`；按钮文案在失败后变“重试导入” |

错误文案映射 `importErrors`：`legacy_interrupted`（旧 CLI 有未完成节点）、`legacy_changed`（旧历史正在变化）、`legacy_empty`（没有可确认的消息）、`legacy_unreadable`（检查点无法读取）；其它 `error_code` 显示“导入未完成，请重试。”。

### R6. 列表与分页渲染

- 顶部标签按钮：`会话列表`、`回收站`、`CLI 历史` + “刷新列表”；`busy` 时禁用。
- `active`：每行标题按钮打开编辑器，右侧“移入回收站”；副标题为 `角色 · 来源 · 检索开/关 / 存储开/关`，来源映射 `legacy → 旧 CLI`、`cli → CLI`、其它 `桌面`。
- `trash`：标题不可点；右侧“恢复”“永久删除”；提示“永久删除会清理该会话的历史、运行和语音资源，角色共享的长期记忆保留。”。
- `legacy`：提示“已确定角色的旧 CLI 会话会自动导入…导入不会执行旧任务或存储记忆。”；无条目且不忙时显示“没有待关联或正在导入的 CLI 历史。”。
- 底部：`cursor` 存在时显示“加载更多”按钮（`load(true)`）。

## 分支与异常链

1. **演示模式**：整页替换为 `Empty`，不渲染任何列表。
2. **版本冲突（保存设置）**：`updateConversation` 抛出服务端错误，编辑器保留编辑并显示消息；需“放弃编辑并重读”。
3. **检查点同步 409**：`applySync` 捕获后写 `syncError` 并提示“请重新预览后再确认。”；本地历史保持不变。
4. **会话执行中**：`active` 为真时禁用检查点预览与确认，并显示说明。
5. **旧 CLI 自动导入**：由 [app 叶子](../app/README.md) R3 的 `refresh()` 扫描完成，页面只展示结果；导入中的条目由 2 秒轮询刷新。
6. **过期响应**：`request.current` 的 ticket 与卸载时的自增共同保证旧响应不覆盖新标签数据。
7. **导入失败**：显示 `importErrors` 或服务端错误；`running` 状态禁用重复提交。
8. **永久删除后**：`manageThread` 内 `forgetThread` 会清理本地消息、阅读位置与语音记录，再 `refresh()` 校准服务端列表。

## 输入输出示例

**示例 1（R3，保存会话设置）**

输入：`id = "t-1"`、`version = 3`、`title = "雨后的傍晚"`、`policy = { retrieval: true, storage: false }`。

请求：`PATCH /api/threads/t-1`，body `{ "expected_version": 3, "title": "雨后的傍晚", "memory_retrieval_enabled": true, "memory_storage_enabled": false }`。

输出：编辑器更新为服务端返回的 `version = 4` 与相同策略，提示“已保存，新的输入使用此记忆策略。”；父组件 `saved()` 重新加载列表。

**示例 2（R3，检查点预览）**

dry-run 响应（`checkpointSyncDto` 节选）：

```json
{
  "dry_run": true,
  "checkpoint_id": "cp-9",
  "latest_message_text": "嗯，已经停了。",
  "delete_count": 2,
  "run_count": 1,
  "preview_truncated": false,
  "version": 7,
  "state": {}
}
```

弹窗显示“将删除 2 条消息（1 次运行），此操作无法撤销。”与两条 `delete_preview`；确认时再调用 `syncCheckpoint(id, 7, false, "cp-9")`。

**示例 3（R5，CLI 历史导入）**

输入：`source_id = "legacy-3"`、`character = "SuLi"`、`title = "CLI 历史"`。

输出：`POST /api/legacy/threads/legacy-3/import` 成功；`changed()` 触发列表刷新与 `workspaceStore.refresh()`，导入后的会话出现在“会话列表”并标注“旧 CLI”。

## 关联文档与验证依据

- 上级：[frontend/README.md](../README.md)
- 同级：[app/README.md](../app/README.md)（`manageThread`/`updateConversation`/`previewCheckpointSync`/`applyCheckpointSync`）· [api/README.md](../api/README.md)（端点表）· [chat/README.md](../chat/README.md)（记忆策略展示）· [settings/README.md](../settings/README.md)（连接与模型设置）
- 服务端会话、检查点同步与旧 CLI 导入：[server/README.md](../../server/README.md) · CLI 断线恢复语义：[cli/README.md](../../cli/README.md)
- 单元测试：[checkpoint-sync.test.ts](../../../frontend/src/app/checkpoint-sync.test.ts)（同步后重置分页/阅读位置、过期响应丢弃、409 后重新预览、拒绝时保留本地历史）
- 端到端测试：[service.spec.ts](../../../frontend/e2e/service.spec.ts)（检查点冲突恢复与另一客户端刷新、记忆开关/CLI 变更/冲突草稿/回收站与恢复共用会话、自动发现 CLI 历史并关联未知角色）
- 历史验收记录（原 `frontend/README.md`，已迁移）：2026-09-22 覆盖会话设置检查点重载、409 冲突需重新预览、同步后重置分页与阅读位置并保留草稿、回收站与恢复、CLI 历史关联导入。
- 未验证项：本页为静态阅读源码与既有测试整理，本次未重新执行测试；旧 CLI 检查点的真实导入行为以 [server/README.md](../../server/README.md) 与 [cli/README.md](../../cli/README.md) 为准。
