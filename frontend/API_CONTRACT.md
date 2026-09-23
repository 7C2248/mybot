# 前后端接入约定

阶段 A、B、C 已实现，完整接口以 [服务说明](../server/README.md) 和运行中的 OpenAPI 为准。`src/shared/api/http.ts` 提供 HTTP/SSE 适配器，`contracts.ts` 使用 Zod 校验响应；演示模式使用 `demo.ts`。桌面默认连接真实服务，浏览器默认演示。服务端持久化线程与正式消息；本机按服务地址保存草稿、阅读位置、偏好和恢复标识，演示与服务缓存独立。桌面原生层管理自动启动、复用和退出，React 查询状态并提供重试入口。

## 线程与消息

| 接口 | 约定 |
| --- | --- |
| `GET /api/threads` | 返回 `id / character_id / title / created_at / updated_at / current_run / latest_run`，按游标分页；最新运行摘要支持新客户端恢复失败状态 |
| `POST /api/threads` | 输入 `character_id / title`，角色绑定不可在同一线程内切换 |
| `GET /api/threads/{id}/messages` | 完整历史，稳定消息 ID；支持 `before` 游标；不能仅从被裁剪的 checkpoint 恢复 |
| `POST /api/threads/{id}/runs` | 输入原始 `text` 与 `client_request_id`；响应 `run_id`；幂等重复请求返回同一运行 |
| `GET /api/runs/{id}/events` | SSE，事件序号支持重连去重；断开连接不取消后台运行 |
| `GET /api/runs/{id}` | 运行快照，用于刷新或重连后恢复 |
| `GET /api/threads/{id}/state` | 已保存的公开状态与本轮检索命中 |
| `GET /api/threads/{id}/memory-status` | `status / job_id`；后台记忆状态，独立于对话运行 |
| `POST /api/runs/{id}/retry` | 新请求键明确重试最近未提交正文的失败运行，复用用户消息 |
| `POST /api/threads/{id}/checkpoint-sync` | `expected_version` 与 `dry_run`；预览最新检查点差异，提交时回传预览的 `checkpoint_id`，只截断检查点最新消息之后的 UI 消息 |

消息 DTO：`id / thread_id / run_id / sequence / role / text / created_at`。`role` 为 `user` 或 `assistant`。输入的 `trim` 仅用于拒绝空白，不修改实际消息。前端只把正文作为文本渲染。历史分页响应 `items / next_cursor`，当前页按 sequence 升序显示，next_cursor 作为下一次 before 读取更早消息。写请求携带 `X-Mybot-Client: mybot-desktop`。

检查点截断保留匹配消息及其之前的历史。若匹配点是用户输入，同轮助手消息与运行被删除，保留输入的 `run_id` 会变为 `null`，消息 ID、正文和来源保持不变；导入消息也允许 `run_id=null`。提交即使没有待删消息也校验预览的 `checkpoint_id`。收到 409 后，确认弹窗显示错误并废弃旧预览，必须重新预览才能确认。客户端检测到会话 `version` 增加时，使消息分页、阅读位置与旧运行缓存失效并重读历史；取消同步前的历史请求，保留草稿。

UI 事件仅暴露：

- `phase`：确实执行到的用户可见阶段，例如 `replying / reviewing / recalling`。
- `message.committed`：检查通过、正式提交并持久化后的角色消息。
- `state.updated`：现有世界、角色与用户字段。
- `memory.retrieved`：真实检索返回的记录；没有事件则不填充“本轮记忆”。
- `run.completed / run.failed`：终态以及可恢复错误。正文已提交后，记忆或语音错误不得引导重新生成正文。

不暴露草稿、内部推理或检查专属参数。回复检查默认执行。每个线程串行运行；浏览器断线后，已提交消息通过稳定 ID 补齐，不能重复生成。

提交请求至获得回执期间显示 `sending`（正在发送）；收到服务的 `queued` 回执才显示“等待执行”，等待的是对话/TTS 执行器。`updating_memory` 显示“正在提交记忆任务”，只对应持久队列交接。后台记忆使用独立执行器，状态为 `disabled / idle / pending / running / failed / completed`，其中完成状态表示最近一项结果。活动会话每 5 秒刷新，并在对话结束时补查；状态读取失败只影响后台提示，不改变主流程状态、连接状态或发送能力。独立进程 Worker 的运行中任务仍按 `pending` 展示为“任务已提交”。

## 角色、记忆与设置

| 接口 | 约定 |
| --- | --- |
| `GET /api/characters` | 已知角色 ID、展示名、可用档案语言和受控图片资源 ID |
| `GET /api/characters/{id}` | 档案正文、版本与 UI 资源配置 |
| `GET /api/resources/{id}` | 限定在角色资源目录中的文件，禁止直接接收任意磁盘路径 |
| `GET /api/characters/{id}/memories` | `query / cursor / limit`；返回原文字段和元数据 |
| `GET /api/characters/{id}/memories/{memory_id}` | 完整原文，ID 仅在所属角色范围内唯一 |
| `GET /api/settings` | 脱敏服务和模型状态；密钥只返回是否已配置 |

记忆 DTO：`id / memory / update_time / importance / event_date / keywords`。前端可将 ID 统一转为字符串；日期、关键词等缺失值显示“未记录”。不存在 `summary` 或 `title` 字段，不调用模型补齐。

编辑与播放界面使用以下接口：

| 接口 | 约定 |
| --- | --- |
| `PUT /api/characters/{id}/profiles/{language}` | `text / expected_version`；原子写回已有语言文件，版本冲突 409，下一任务使用新档案 |
| `GET /api/settings/models` | `nodes / saved_version / active_version / pending_changes / runtime_busy / effective_from` |
| `PUT /api/settings/models` | `nodes / expected_version`；只保存公开节点配置，不修改检查专属设置、密钥或本地路径 |
| `POST /api/settings/models/apply` | `expected_version`；任务执行中返回 409，应用后下一任务生效 |
| `POST /api/messages/{id}/speech` | `client_request_id`；为正式 assistant 消息排队合成，返回语音任务 |
| `GET /api/speech/{id}` | `status / resource_id / resource_url / error_code` 等字段；终态后停止轮询 |
| `GET /api/audio/resources/{id}` | 已注册 WAV 音频，可用于播放器和 Range 请求 |
| `GET /api/service` | 托管/手工启动模式、对话状态及独立的 `memory_runtime_ready / memory_runtime_error` |

退出接口仅供持有 owner token 的原生桌面服务管理器调用，不向 React 暴露凭据。档案编辑开始时记录版本，后台刷新不替换该版本；冲突保留草稿，用户可读取最新文件并比较。模型修改分别保存与应用，任务忙碌或版本冲突按服务错误展示；没有真实推理验证的配置不显示“测试连接成功”。TTS 失败单独重试，不重发对话。

## 恢复与验证

连接偏好键为 `mybot.frontend.connection.v1`，演示缓存为 `mybot.frontend.workspace.v1`，服务缓存为 `mybot.frontend.service.v1:<规范服务地址>`。缓存不保存服务消息列表。发送前记录原文与请求键，超时或响应丢失后复用原键，恢复期间保留下一条草稿；明确拒绝的输入也保留供用户恢复。

SSE 支持分片 UTF-8、CRLF、多行 data、心跳和事件序号去重。每次订阅前读取运行快照和状态，断线后退避重连；切换服务会终止旧订阅，忽略旧请求回包。正式消息按 ID 合并、sequence 排序。历史分页保存可见消息 ID 和偏移，浮窗只改变显示与窗口状态，不重发输入。

已覆盖 19 项单元测试、9 项演示浏览器流程、9 项实际 HTTP/PostgreSQL 浏览器流程，以及打包桌面的对话。后台记忆验收使用受控处理器，确认记忆尚未结束时下一轮对话已完成。运行命令和具体边界见 [README](README.md)。音频端到端流程使用受控 WAV，真实 TTS 权重尚未验收。


## 会话策略与历史管理

`Thread` 新增 `version / memory_policy_version / memory_retrieval_enabled / memory_storage_enabled / deleted_at / source / history_notice`。新建/导入默认两项记忆关闭，升级已有会话保留原开启策略。会话设置通过 `PATCH /api/threads/{id}` 提交 `expected_version` 以及标题和两个开关；CLI 的 `/memory` 与启动参数使用同一接口。409 时保留草稿并要求重读。

`DELETE /api/threads/{id}` 移入回收站，`GET /api/threads?deleted=true` 浏览，`POST /api/threads/{id}/restore` 恢复，`DELETE /api/threads/{id}/purge` 永久删除；三个写操作均携带 `expected_version`。永久删除在界面二次确认，保留角色共享长期记忆。活动对话/语音期间禁止设置和删除。

会话设置的“重新加载检查点”先以 `dry_run=true` 读取最新检查点并展示将删除的消息，确认后携带预览返回的 `checkpoint_id` 提交。检查点中不存在的更早消息因上下文裁剪而合法保留，只删除检查点最新消息之后的尾部；长期记忆不回滚。检查点缺失、无确认消息、最新消息不在 UI、预览后变化、记忆任务未完成分别返回 409，界面保留当前历史并提示原因。

`GET /api/legacy/threads` 分页发现旧 CLI；明确角色的历史自动提交 `POST /api/legacy/threads/{source}/import`，否则提供预览与角色关联。导入状态为 `queued / running / completed / failed`；失败可重试。导入后出现在同一会话列表，消息 `run_id` 可为 null，`source=legacy`，不可信时间使用 `timestamp_estimated` 标记，并展示 `history_notice`。不会把草稿或工具结果当正式回复，不自动执行未完成的旧节点。

新建会话和会话设置都提供两个独立开关。聊天区显示当前策略；每 5 秒刷新可发现 CLI 的设置和新运行。只存不读时仅新增记忆；重新开启存储不补存关闭期间的消息，详见 [服务策略说明](../server/README.md)。
