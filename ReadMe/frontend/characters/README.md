# 角色档案与素材（characters/）

## 职责与入口

`CharactersPage` 是“角色”一级导航页面：按角色读取 Markdown 档案（按语言切换）、编辑并写回（服务模式带版本校验），并在“图片资源”标签选择立绘素材。模块类型为 React 页面组件，不是 Agent 图节点；档案内容的权威来源与版本规则在 [server/README.md](../../server/README.md) 的角色服务中。

- **源码**：[CharactersPage.tsx](../../../frontend/src/features/characters/CharactersPage.tsx)
- **入口**：`App` 在 `page === 'characters'` 时渲染 `<CharactersPage create={startNew} />`，见 [app 叶子](../app/README.md) R4。
- **上游**：`App`、`WorkspaceStore`（`characters`、`profileOverrides`、`preferences`、`mode`）；**下游**：`workspaceStore.saveProfile`、`workspaceStore.reloadCharacter`、`workspaceStore.setPreferences`、`App.startNew`（新建对话）。
- **触发时机**：点击“开始对话”、切换角色/语言、进入编辑、保存、版本冲突后重读、选择立绘素材。

## 调用链总览

```text
R1 挂载与角色解析：characters[0] 或选中项 → activeLanguage → profile → sprite
R2 角色选择与“开始对话”：select(id) 或 create(character.id) → App.startNew → 新建对话对话框
R3 档案编辑链：编辑 → saveProfile(id, language, draft, version) → 成功/版本冲突
R4 版本冲突恢复：读取最新版本 → reloadCharacter(id) → 展示服务器最新档案对照 → 再次保存
R5 图片资源链：assets 标签 → setPreferences({ sprites: { [角色]: { ...sprite, assetId } } })
```

本组件没有工厂或构建阶段：模块只有 `CharactersPage` 一个导出函数组件与若干 `useState`。

## 运行链

### R1. 挂载与角色解析

- 定位与签名：`CharactersPage({ create }: { create: (id: string) => void })`。
- 调用方与条件：`App` 渲染，`characters` 快照更新后重渲染。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `create` | `(id: string) => void` | `App.startNew` | 必填 | 以当前角色新建会话 |
| `characters` | `Character[]` | `useWorkspace()` | 初始为空 | 角色列表与档案 |
| `loading` | `boolean` | 快照 | 初始 `true` | 空列表时的提示文案 |
| `profileOverrides` | `Record<string, Record<string, string>>` | 快照（演示模式本地草稿） | 默认 `{}` | 覆盖服务端档案展示 |
| `preferences.sprites` | `Record<string, SpriteSettings>` | 快照 | 默认 `{}` | 立绘素材选择 |
| `mode` | `'demo' \| 'service'` | 快照 | 必填 | 决定按钮文案与保存去向 |
| `selectedId` | `string` | `useState(characters[0]?.id ?? '')` | 空串 | 当前角色 |
| `language` | `string` | `useState('zh')` | `'zh'` | 首选档案语言 |
| `editing` / `draft` / `saved` / `version` / `error` / `busy` / `latest` | 编辑状态 | `useState` | 见各分支 | 编辑、版本与冲突展示 |

功能与内部调用：
1. `character = characters.find(id === selectedId) ?? characters[0]`；不存在时渲染 `Empty`（`loading` 时“正在读取角色…”，否则“暂无角色档案”，提示将档案放入 `Character` 目录后重新准备本地资源）。
2. `activeLanguage = character.profiles[language] !== undefined ? language : Object.keys(character.profiles)[0]`：所选语言缺失时回退到档案的第一个语言键。
3. `profile = profileOverrides[character.id]?.[activeLanguage] ?? character.profiles[activeLanguage] ?? ''`：演示草稿优先于服务端内容。
4. `sprite = preferences.sprites[character.id] ?? spriteSchema.parse({})`：未配置时使用 `enabled: false, assetId: '', side: 'right', scale: 100, mirror: false`。
5. 渲染 `character-picker`（编辑中禁用）、`page-heading`（名称 + “开始对话”）、标签 `角色档案` / `图片资源`（编辑中禁用）。

输出：页面结构；无副作用。边界：空角色列表、缺少语言、无图片资源都有显式降级文案。

### R2. 角色选择与新建对话

| 交互 | 调用 | 输入 | 行为与去向 |
| --- | --- | --- | --- |
| 点击角色卡片 | `select(item.id)`、`setSaved(false)` | 角色 id | 切换当前角色；编辑中禁用 |
| 点击“开始对话” | `create(character.id)` | 当前角色 id | `App.startNew(id)` 打开“新建对话”对话框，预选该角色（见 [app 叶子](../app/README.md) R4） |

### R3. 档案编辑与保存

- 定位与签名：`workspaceStore.saveProfile(id: string, language: string, text: string, version?: string)`（见 [app 叶子](../app/README.md) R8）。
- 调用方与条件：编辑态点击“写回角色档案”（服务模式）/“保存本地草稿”（演示模式）；`!draft.trim()` 时禁用。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `character.id` | `string` | 当前角色 | 必填 | 目标档案 |
| `activeLanguage` | `string` | R1 | 必填 | 目标语言文件 |
| `draft` | `string` | 编辑文本域 | `trim()` 非空 | 新档案正文 |
| `version` | `string?` | 进入编辑时取自 `character.version` | 服务模式必须存在 | 乐观并发校验；缺失时 store 抛“请先重新读取档案版本。” |

功能与内部调用（组件侧）：
1. 点击编辑：`setDraft(profile)`、`setVersion(character.version)`、`setLatest(undefined)`、清空错误、`setEditing(true)`。
2. 点击保存：`setBusy(true)` → `workspaceStore.saveProfile(...)` → 成功 `setEditing(false)`、`setSaved(true)`；失败 `setError(message)`，草稿保留。
3. 演示模式：store 只写 `profileOverrides`，展示“已保存本地档案草稿，原始 Markdown 文件保持原样。”；服务模式成功展示“档案已写回，下一个任务使用新版本。”。

输出：`characters` 中该角色被服务端返回值替换（服务模式）；`profileOverrides` 更新（演示模式）。异常与边界：版本冲突由服务端返回错误，组件保留草稿并给出重读入口。

### R4. 版本冲突恢复

- 触发条件：`error` 非空且 `mode === 'service'` 时，错误行旁显示“读取最新版本”按钮。

| 步骤 | 调用 | 输入 | 行为与输出 |
| --- | --- | --- | --- |
| 1 | `workspaceStore.reloadCharacter(character.id)` | 角色 id | `api.character(id)` 拉取最新档案并替换 `characters` 快照；演示模式直接返回 `undefined` |
| 2 | `setLatest(value.profiles[activeLanguage])`、`setVersion(value.version)` | 返回值 | 记录服务器最新版本与对应语言正文 |
| 3 | 渲染 `<details open>` | `latest` | 标题“服务器上的最新档案（供对照）”，与编辑草稿并排显示 |
| 4 | 再次点击保存 | 原 `draft` + 新 `version` | 重新调用 `saveProfile` |

异常与边界：`reloadCharacter` 失败时把错误写入 `error` 且保留草稿；提示文案为“已读取最新版本，编辑草稿仍保留。请对照后再保存。”。

### R5. 图片资源与立绘选择

- 触发条件：点击“图片资源”标签（编辑中禁用）。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `character.assets` | `{ id, name, url }[]` | 快照（服务端已绝对化 URL） | 可能为空 | 素材网格 |
| `sprite` | `SpriteSettings` | R1 | 默认未选 | 当前选中素材 |

功能与内部调用：每条素材是一个按钮，`aria-pressed={sprite.assetId === asset.id}`；点击调用 `workspaceStore.setPreferences({ sprites: { ...preferences.sprites, [character.id]: { ...sprite, assetId: asset.id } } })`，保留该角色的其他立绘设置。无素材时显示 `Empty`：“缺少图片时以姓名缩写显示头像。”。

输出：偏好持久化；实际显示位置与开关在“设置 → 立绘显示”和对话工作台生效，见 [settings 叶子](../settings/README.md) 与 [chat 叶子](../chat/README.md) R1。

## 分支与异常链

1. **无角色**：显示 `Empty`，不渲染编辑区。
2. **语言缺失**：`activeLanguage` 回退到档案的第一个语言键；下拉框只列出实际存在的语言。
3. **编辑中**：角色选择与“图片资源”标签禁用，避免切换角色丢失草稿。
4. **保存失败（含版本冲突）**：错误写入 `error`，编辑态与草稿保留；服务模式提供“读取最新版本”。
5. **演示模式**：不调用服务端，只保存 `profileOverrides`；不修改 `Character/` 下的原始 Markdown。
6. **无版本号**：服务模式调用 `saveProfile` 时 `version` 为空会抛“请先重新读取档案版本。”，提示用户先重读。
7. **素材未选择**：`assetId` 为空时工作台不显示立绘，仅用姓名缩写头像。

## 输入输出示例

**示例 1（R3，成功写回）**

输入：`character.id = "SuLi"`、`activeLanguage = "zh"`、`draft = "（更新后的档案正文）"`、`version = "3"`。

请求：`PUT /api/characters/SuLi/profiles/zh`，body `{ "expected_version": "3", "text": "（更新后的档案正文）" }`。

输出：组件 `setSaved(true)` 并显示“档案已写回，下一个任务使用新版本。”；`characters` 快照中的 `version` 由服务端返回的新值替换。

**示例 2（R4，版本冲突）**

输入：`expected_version = "3"`，服务端实际为 `"4"`。

输出：`error` 显示服务端消息，草稿保留；点击“读取最新版本”后 `latest` 展示 v4 正文，`version` 更新为 `"4"`，再次保存成功。

**示例 3（R5，选择立绘素材）**

输入：角色 `SuLi` 的素材 `{ id: "suli-2", name: "suli_02" }`。

输出（偏好增量）：

```json
{ "sprites": { "SuLi": { "enabled": false, "assetId": "suli-2", "side": "right", "scale": 100, "mirror": false } } }
```

在“设置 → 立绘显示”打开开关后，工作台按该素材显示立绘。

## 关联文档与验证依据

- 上级：[frontend/README.md](../README.md)
- 同级：[app/README.md](../app/README.md)（`saveProfile`/`reloadCharacter`/`startNew`）· [settings/README.md](../settings/README.md)（立绘显示）· [chat/README.md](../chat/README.md)（工作台立绘渲染）· [memory/README.md](../memory/README.md)（角色记忆）
- 服务端角色目录与档案写回：[server/README.md](../../server/README.md)（`CharacterCatalog`、版本化档案、受控资源 ID）
- 端到端测试：[service.spec.ts](../../../frontend/e2e/service.spec.ts)（“profile conflict retains the draft and model settings save and apply independently”）、[workspace.spec.ts](../../../frontend/e2e/workspace.spec.ts)（“browses raw memories and selects a real sprite”）
- 历史验收记录（原 `frontend/README.md`，已迁移）：2026-09-21 覆盖档案版本冲突；2026-09-22 覆盖受控测试实现下的档案与模型流程。演示模式档案只保存本地草稿。
- 未验证项：本页为静态阅读源码与既有测试整理，本次未重新执行测试；真实模型与真实 TTS 权重未验收。
