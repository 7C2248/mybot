# 记忆原文浏览（memory/）

## 职责与入口

`MemoryPage` 是“记忆”一级导航页面：按角色检索长期记忆原文，提供文本搜索、游标分页、两行预览与完整原文详情。页面只展示服务端返回的真实字段，不生成摘要、不修改数据；模块类型为 React 页面组件，不是 Agent 图节点。记忆的存储与检索实现见 [agent/memory/README.md](../../agent/memory/README.md) 与 [server/README.md](../../server/README.md)。

- **源码**：[MemoryPage.tsx](../../../frontend/src/features/memory/MemoryPage.tsx)
- **入口 1**：`App` 在 `page === 'memory'` 时渲染 `<MemoryPage key={\`${thread?.characterId}-${memoryId}\`} initialCharacter={...} initialMemory={memoryId} />`，见 [app 叶子](../app/README.md) R4。
- **入口 2（深链）**：对话检查器“记忆”标签点击命中项时 `openMemory(id)` 把 `memoryId` 写入 `App` 状态并跳转，`key` 变化导致页面重挂载并以 `initialMemory` 打开详情。见 [chat 叶子](../chat/README.md) R1。
- **上游**：`App`、`WorkspaceStore`（`characters`、`mode`）；**下游**：`HttpService.memories/memory`（服务模式）或 `demoService.memories`（演示模式）。
- **触发时机**：进入页面、切换角色、输入搜索词（250ms 防抖）、翻页、点击条目、点击“刷新记忆”。

## 调用链总览

```text
R1 挂载：initialCharacter → character；initialMemory → selected；cursors=[undefined]
R2 搜索防抖：query 变化 → 250ms 后 setSearch + 重置分页
R3 列表读取：api.memories(character, search, cursor, signal) → setMemories/setNext
R4 详情读取：selected 变化 → api.memory(character, id) → Dialog
R5 分页：cursor = cursors[page]；下一页把 next 追加进 cursors 并 page+1；上一页 page-1
R6 演示分支：本地过滤 + 每页 10 条；详情从同一数组查找
```

本组件没有工厂或构建阶段：模块只有 `MemoryPage` 一个导出组件与若干 `useState`/`useEffect`。

## 运行链

### R1. 挂载与初始状态

- 定位与签名：`MemoryPage({ initialCharacter, initialMemory }: { initialCharacter: string; initialMemory?: string })`。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `initialCharacter` | `string` | `App`：`thread?.characterId ?? characters[0]?.id ?? ''` | 可为空串 | 初始角色 |
| `initialMemory` | `string?` | `App.memoryId` | 可选 | 深链打开的记忆 id |
| `characters` / `mode` | `Character[]` / `'demo' \| 'service'` | `useWorkspace()` | 必填 | 角色下拉与分支 |
| `character` | `string` | `useState(initialCharacter)` | 必填 | 当前角色 |
| `query` / `search` | `string` | `useState('')` | 空 | 输入值与防抖后的检索词 |
| `page` | `number` | `useState(0)` | 0 | 当前页 |
| `cursors` | `(string \| undefined)[]` | `useState([undefined])` | 第 0 页无游标 | 每页起始游标 |
| `selected` | `string?` | `useState(initialMemory)` | 可选 | 详情弹窗 |
| `refresh` | `number` | `useState(0)` | 0 | 手动刷新触发计数 |

角色下拉选项为 `[...new Set([initialCharacter, ...characters.map(item => item.id)])].filter(Boolean)`：即使角色列表尚未加载，也保留深链来源的角色。

### R2. 搜索防抖

- 定位与签名：`useEffect(..., [query])`。

| 输入 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `query` | `string` | 搜索输入框 | `maxLength = 1000` | 用户输入 |

功能：设置 250ms 定时器，到期后 `setSearch(query)`、`setPage(0)`、`setCursors([undefined])`；依赖变化时清除上一个定时器。搜索只按原文、编号或关键词做文本匹配，不生成摘要。

### R3. 列表读取

- 定位与签名：`useEffect(..., [api, character, search, page, cursor, refresh])`，内部异步函数 + `AbortController`。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `character` | `string` | R1 | 为空时直接返回 | 角色范围 |
| `search` | `string` | R2 | 可为空 | 过滤词 |
| `cursor` | `string?` | `cursors[page]` | 第 0 页为 `undefined` | 服务端分页游标 |

功能与内部调用：
1. 每次依赖变化先 `setError('')`、`setMemories([])`、`setLoading(true)`，并中止上一次请求。
2. 服务模式：`api.memories(character, search, cursor, signal)` → `setMemories(result.items)`、`setNext(result.next_cursor)`。
3. 演示模式：`api.memories(character)` 取全部示例，按 `` `${memory.id} ${memory.memory} ${memory.keywords.join(' ')}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()) `` 过滤，再 `slice(page * 10, (page + 1) * 10)`；`next = all.length > (page + 1) * 10 ? String(page + 1) : null`。
4. 异常：`setError(errorText(error))`；`finally` 置 `loading = false`。请求被中止时不再写状态。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `memories` | `Memory[]` | 请求成功 | 覆盖写为当前页 | 列表与计数 |
| `next` | `string \| null` | 请求成功 | 下一页游标 | 分页按钮 |
| `error` | `string` | 请求失败 | 覆盖写 | 错误提示与“重试读取” |

### R4. 详情读取

- 定位与签名：`useEffect(..., [api, selected, character, refresh])`。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `selected` | `string?` | 列表点击或 `initialMemory` | 空则关闭详情 | 记忆 id |
| `character` | `string` | R1 | 必填 | 角色范围 |

功能与内部调用：清空 `detail` 与 `detailError`；服务模式 `api.memory(character, selected, signal)`，演示模式从 `api.memories(character)` 查找；找不到时 `setDetailError('该记忆不存在。')`；异常写 `detailError`。弹窗渲染 `Dialog`：标题“记忆 #id”、`hint`（角色 · 完整原文）、`memory-original`（完整原文）、`memory-metadata`（事件日期/重要程度/关键词/更新时间，空值“未记录”）；加载中显示“正在读取完整原文…”。

### R5. 分页与刷新

| 交互 | 调用 | 输入 | 行为 |
| --- | --- | --- | --- |
| 下一页 | `setCursors([...cursors.slice(0, page + 1), next ?? undefined])`、`setPage(page + 1)` | `next` 非空 | 保存下一页游标并前进；`loading` 或 `!next` 时禁用 |
| 上一页 | `setPage(page - 1)` | `page > 0` | 回到上一页；`loading` 或第 0 页时禁用 |
| 刷新记忆 | `setRefresh(v => v + 1)` | 无 | 重新执行 R3/R4；错误提示内也提供“重试读取” |
| 切换角色 | `setCharacter(value)`、`setPage(0)`、`setCursors([undefined])`、`setSelected(undefined)` | 下拉值 | 重置分页与详情 |

分页控件在 `page > 0 || next` 时出现，显示“第 N 页”。

### R6. 列表渲染与空状态

- 每行：`记忆 #{memory.id}`、`text-clamp` 两行预览、`event_date ?? '日期未记录'`、`重要程度 {importance ?? '未记录'}`、关键词 `join(' / ')`（无关键词则不显示）。
- 空状态：有搜索词时“没有匹配的记忆”，否则“暂无记忆”。
- 顶部提示：`{character} · 当前页 {memories.length} 条 · 新建对话继续共享该角色记忆`，明确删除会话不等于删除角色记忆。

## 分支与异常链

1. **角色为空**：读取链直接返回，保持空列表与加载结束状态。
2. **请求失败**：显示错误与“重试读取”；请求被新依赖中止时不显示错误。
3. **详情不存在**：`detailError` 为“该记忆不存在。”，弹窗保持打开。
4. **演示模式**：不访问服务端；搜索范围包含编号与关键词，分页在本地数组上完成。
5. **深链**：`initialMemory` 指向不存在或不属于当前角色的 id 时，详情弹窗显示错误，列表仍可用。
6. **空字段**：`importance`、`event_date`、`update_time`、`keywords` 为空时统一显示“未记录”，不填充推测值。
7. **组件卸载/依赖切换**：两个 effect 都以 `AbortController` 清理，避免过期响应覆盖新状态。

## 输入输出示例

**示例 1（R3，服务模式列表）**

输入：`character = "SuLi"`、`search = "书店"`、`cursor = undefined`。

请求：`GET /api/characters/SuLi/memories?query=书店`。

输出（`setMemories` 的单项）：

```json
{
  "id": "12",
  "characterId": "SuLi",
  "memory": "傍晚的雨停后，我们决定去街角的旧书店。\n她提到书店里有一只橘猫，喜欢趴在窗边的纸箱上。",
  "importance": 6,
  "event_date": "2026-09-12",
  "update_time": "2026-09-12T18:42:00+08:00",
  "keywords": ["书店", "橘猫"]
}
```

列表显示“记忆 #12”、两行预览、`2026-09-12 · 重要程度 6 · 书店 / 橘猫`。

**示例 2（R4，详情）**

点击“记忆 #23”（示例数据中 `importance/event_date/keywords` 全空）后弹窗显示完整原文，元数据为：

```text
事件日期 未记录
重要程度 未记录
关键词   未记录
更新时间 未记录
```

**示例 3（R5，游标分页）**

第 0 页返回 `next_cursor = "c-2"`，点击下一页后 `cursors = [undefined, "c-2"]`、`page = 1`，请求带 `cursor=c-2`；返回 `next_cursor = null` 时“下一页”禁用。

## 关联文档与验证依据

- 上级：[frontend/README.md](../README.md)
- 同级：[chat/README.md](../chat/README.md)（检索命中深链）· [app/README.md](../app/README.md)（`memoryId` 状态）· [api/README.md](../api/README.md)（`memories`/`memory` 端点）· [characters/README.md](../characters/README.md)（角色范围）
- 记忆存储与检索实现：[agent/memory/README.md](../../agent/memory/README.md) · 服务端只读浏览：[server/README.md](../../server/README.md)
- 端到端测试：[service.spec.ts](../../../frontend/e2e/service.spec.ts)（“queries original memories with cursor pages and role-scoped detail”）、[workspace.spec.ts](../../../frontend/e2e/workspace.spec.ts)（“browses raw memories and selects a real sprite”）
- 历史验收记录（原 `frontend/README.md`，已迁移）：2026-09-21 覆盖原始记忆分页；2026-09-22 覆盖记忆开关、CLI 变更、冲突草稿、回收站与恢复共用同一会话。记忆使用受控测试实现，未调用真实模型。
- 未验证项：本页为静态阅读源码与既有测试整理，本次未重新执行测试；摘要/编辑/删除能力当前不存在，也未列入实现。
