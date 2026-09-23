# 语音与音频资源：SpeechRepository 与 SpeechService

## 职责与入口

本页覆盖 `server/repositories/speech.py`、`server/services/speech.py` 与 `server/routes/speech.py`：为已提交的正式角色回复排队 TTS 任务、在对话执行线程内合成 WAV、登记音频资源并通过受控接口读取。服务不调用声卡播放，只生成浏览器可播放文件。

| 文件 | 类型 | 职责 |
| --- | --- | --- |
| [server/repositories/speech.py](../../../server/repositories/speech.py) | 仓储 | `SpeechRepository`：任务入队/查询/资源校验/领取/完成/恢复 |
| [server/services/speech.py](../../../server/services/speech.py) | 服务 | `SpeechService.execute`、`audio_path`、`write_wave` |
| [server/routes/speech.py](../../../server/routes/speech.py) | 路由 | 任务提交/查询、音频资源读取（Range 由 Starlette `FileResponse` 支持） |

上游：`RunRuntime._serve` 主循环在无对话任务时领取语音任务（见 [runtime 叶子](../runtime/README.md)）；下游：`agent/node/tts.py` 的 `create_tts_node`（见 [node/tts 叶子](../../agent/node/tts/README.md)）、`data/audio/` 文件系统。

## 调用链总览

```text
构建阶段
B1 SpeechRepository（继承 RunRepository，无自定义 __init__）
B2 SpeechService.__init__ ── 保存 audio_root/catalog/models

运行阶段
R1 POST /api/messages/{message_id}/speech → SpeechRepository.enqueue ── 幂等与互斥入队
R2 RunRuntime 主循环 → SpeechRepository.claim ── FOR UPDATE SKIP LOCKED 领取
R3 SpeechService.execute ── 读取语音档案 → create_tts_node(audio_sink=write_wave) → 合成
  R3.1 audio_path ── 目录边界与符号链接校验
  R3.2 write_wave ── 校验 + 单声道 16bit WAV + 原子替换
R4 SpeechRepository.finish ── 成功登记 resource_id / 失败记错误
R5 GET /api/speech/{job_id} → SpeechRepository.get ── 任务状态与音频 URL
R6 GET /api/audio/resources/{resource_id} → SpeechRepository.resource + audio_path → FileResponse（Range）
R7 SpeechRepository.recover ── 启动时把遗留 running 标记 interrupted
```

## 构建链

### B1. `SpeechRepository`

- 定位与签名：[server/repositories/speech.py](../../../server/repositories/speech.py) 的 `class SpeechRepository(RunRepository)`，无自定义 `__init__`。
- 调用方式：HTTP 路径 `SpeechRepository(database.pool)`；执行线程 `SpeechRepository(pool, connection=所有权连接, connection_lock=checkpointer.lock)`（与运行写入共用连接，避免所有权丢失后换连接继续写）。`dto` 为静态方法：`dict(row, resource_url=f"/api/audio/resources/{row['resource_id']}" if row.get("resource_id") else None)`。

### B2. `SpeechService.__init__`

- 定位与签名：[server/services/speech.py](../../../server/services/speech.py) 的 `def __init__(self, root, catalog, models)`。
- 输入：`root`（`settings.audio_root`）、`catalog`（`CharacterCatalog`）、`models`（`ModelSettingsService`）。输出：服务实例，无副作用。

## 运行链

### R1. `SpeechRepository.enqueue`

- 定位与签名：`async def enqueue(self, message_id, request_id)`。
- 调用方：`POST /api/messages/{message_id}/speech`（先 `require_runtime`：运行执行器就绪且模型配置可快照）。

| 输入 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `message_id` | `UUID` | 路径 | 必须是 assistant 消息 | 定位回复 |
| `request_id` | `str` | `RequestKey.client_request_id` | 1..200 非空白 | 幂等键 |

功能（单事务）：
1. 查消息：不存在或 `role != 'assistant'` → 404 `message_not_found`（“只能为已提交的角色回复生成语音”）。
2. `_thread(conn, message.thread_id, lock=True)` 锁线程（已删除线程 → 404）；`SELECT id FROM messages WHERE id=%s FOR UPDATE` 锁消息。
3. `len(message["text"]) > 10000` → 413 `speech_text_too_long`。
4. 相同 `(message_id, client_request_id)` 的任务已存在 → 返回其 DTO（请求键幂等）。
5. 该消息已有 `queued/running` 任务 → 复用该任务 DTO（同一消息同时只有一个活动任务）。
6. 否则 `INSERT INTO speech_jobs(id, message_id, client_request_id) VALUES (uuid4(), ...)`，返回 DTO。

输出：`SpeechJob`（`status='queued'`、`resource_url=None`）。副作用：`speech_jobs` 一行。

### R2. `SpeechRepository.claim`

- 定位与签名：`async def claim(self)`。
- 调用方：`RunRuntime._serve` 主循环（对话任务为空时）。

功能（单事务）：`SELECT * FROM speech_jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED`；无行返回 `None`；否则置 `status='running'`，再 `JOIN messages/threads` 返回带 `text` 与 `character_id` 的行（供 `SpeechService.execute`）。
输出：任务上下文行或 `None`。

### R3. `SpeechService.execute`

- 定位与签名：`async def execute(self, job)`。
- 调用方：`RunRuntime._serve` 主循环（领取成功后；同一循环串行执行对话与语音）。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `job["character_id"]` | `str` | `claim` 联结 threads | 已注册 | 选择语音档案 |
| `job["text"]` | `str` | `claim` 联结 messages | ≤10000（入队时校验） | 待合成正文 |
| `job["id"]` | `UUID` | `speech_jobs` | 非空 | 资源文件名与资源 ID |

功能与内部调用：
1. `text = self.catalog.voice_profile(job["character_id"])`：优先角色 `tts.md`，缺失时回退 `zh` 档案或首个档案（实现见 [characters 叶子](../characters/README.md)）。
2. `target = audio_path(self.root, job["id"])`（R3.1）。
3. `data, _ = self.models.snapshot()`；`with model_config_scope(data):` 内调用 `create_tts_node(job["character_id"], character_file=text, audio_sink=lambda waveforms, rate, pauses: write_wave(target, waveforms, rate, pauses))`；`await node({"need_tts": True, "messages": [AIMessage(content=job["text"])]})`。
4. 节点行为（权威说明见 [node/tts 叶子](../../agent/node/tts/README.md)）：解析对白、生成语气/停顿计划、`asyncio.to_thread` 合成；因为传入了 `audio_sink`，不调用 `sounddevice` 播放；回复无对白时节点抛 `ValueError("speech_empty")`。

输出：无返回值；副作用为 WAV 文件写入。异常：合成/写入异常向上抛给主循环，主循环 `finish(error="speech_failed")`。

#### R3.1 `audio_path`

- 定位与签名：[server/services/speech.py](../../../server/services/speech.py) 的 `def audio_path(root: Path, resource_id) -> Path`。
- 功能：`base = root.resolve()`；`path = base / f"{resource_id}.wav"`；若 `path.resolve()` 不在 `base` 内或 `path.is_symlink()` → 404 `resource_not_found`。
- 输出：受控路径。该函数同时被路由 R6、legacy 音频清理与检查点截断后的清理流程使用。

#### R3.2 `write_wave`

- 定位与签名：`def write_wave(path: Path, waveforms, sample_rate, pauses)`（同步，由节点在线程中调用）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `path` | `Path` | 必填 | `audio_path` 结果 |
| `waveforms` | `list[np.ndarray]` | 必填且非空 | 每段对白采样 |
| `sample_rate` | `int` | 必填，8000..192000 | 采样率 |
| `pauses` | `list[float]` | 必填 | 段间停顿秒数（每段取 `max(0, min(3, float(pauses[i])))`） |

功能：
1. 校验采样率范围与 `waveforms` 非空，否则 `ValueError("Invalid synthesized audio")`。
2. `path.parent.mkdir(parents=True, exist_ok=True)`；`tempfile.mkstemp(suffix=".wav.tmp", dir=path.parent)`。
3. `wave.open(output, "wb")`：单声道（`setnchannels(1)`）、16 位（`setsampwidth(2)`）、`setframerate(sample_rate)`。
4. 逐段：`np.asarray(samples, dtype=np.float32)`；要求一维且全为有限值（否则 `ValueError("Invalid waveform")`）；`np.clip(samples, -1, 1) * 32767` 转小端 `<i2` 写入；段间写入 `b"\0\0" * int(sample_rate * pause)` 静音。
5. `flush` + `os.fsync` → `os.replace(temporary, path)` 原子替换；`finally` 删除残留临时文件。

输出：无返回值。格式：PCM 16-bit 单声道 WAV。

### R4. `SpeechRepository.finish`

- 定位与签名：`async def finish(self, job_id, *, error=None)`。
- 功能：`UPDATE speech_jobs SET status=%s, error_code=%s, resource_id=%s, finished_at=now() WHERE id=%s AND status='running'`；成功时 `status='completed'`、`resource_id=job_id`（资源 ID 即任务 ID，文件名 `<job_id>.wav`）；失败时 `status='failed'`、`resource_id=NULL`、`error_code` 为传入值（主循环固定 `speech_failed`）。
- 输出：无。条件 `status='running'` 保证重复调用不覆盖终态。

### R5. `SpeechRepository.get`

- 定位与签名：`async def get(self, job_id)`。
- 功能：`JOIN messages/threads`，要求 `threads.deleted_at IS NULL`；无行 → 404 `speech_not_found`；返回 `dto(row)`。
- 输出：`SpeechJob`（成功后含 `resource_url`）。

### R6. `SpeechRepository.resource` 与音频读取

- 定位与签名：`async def resource(self, resource_id)`。
- 功能：`JOIN messages/threads`，条件 `s.resource_id=%s AND s.status='completed' AND t.deleted_at IS NULL`；无行 → 404 `resource_not_found`。
- 路由 `GET /api/audio/resources/{resource_id}`：先 `resource()`（数据库登记校验），再 `audio_path(settings.audio_root, resource_id)`，文件不存在 → 404；返回 `FileResponse(path, media_type="audio/wav", headers={"X-Content-Type-Options":"nosniff"})`。HTTP `Range` 请求由 Starlette `FileResponse` 原生处理（服务端不自行解析），回收站会话的音频不可读。

### R7. `SpeechRepository.recover`

- 定位与签名：`async def recover(self)`。
- 调用方：`RunRuntime._serve` 初始化阶段（在对话启动恢复之前）。
- 功能：`UPDATE speech_jobs SET status='interrupted', error_code='service_interrupted', finished_at=now() WHERE status='running'`。
- 语义：服务崩溃/退出时正在合成的语音不自动重试；用户可用新请求键重试语音，不重新生成对话。

## 分支与异常链

| 条件 | 处理 | 终点 |
| --- | --- | --- |
| 消息不存在或非 assistant | 404 `message_not_found` | R1 |
| 正文超过 10000 字符 | 413 `speech_text_too_long` | R1 |
| 相同请求键 | 返回原任务 | R1 步骤 4 |
| 同消息已有活动任务 | 复用该任务 | R1 步骤 5 |
| 会话在回收站 | 任务查询/资源读取 404 | R5/R6 |
| 合成无对白（`audio_sink` 模式） | 节点抛 `ValueError("speech_empty")` → 任务 `failed` | R3/R4 |
| 合成或写文件失败 | 任务 `failed`，`error_code='speech_failed'`；音频文件可能不存在 | R4 |
| 关闭时合成被取消 | 主循环 `recover()` 后重新抛出；下次启动统一 `interrupted` | [runtime 叶子](../runtime/README.md) R1 |
| 服务启动时遗留 `running` | `recover()` 标记 `interrupted` | R7 |
| 资源路径越界或符号链接 | `audio_path` 抛 404 | R3.1/R6 |
| 采样率/波形非法 | `write_wave` 抛 `ValueError` | R3.2 |

## 输入输出示例

提交（R1）：

```http
POST /api/messages/9b1c.../speech
X-Mybot-Client: mybot-desktop
{"client_request_id": "speech-0001"}
```

```json
{"id": "c2d3...", "message_id": "9b1c...", "status": "queued",
 "resource_id": null, "resource_url": null, "error_code": null,
 "created_at": "2026-09-23T10:05:00+08:00", "finished_at": null}
```

完成后查询（R5）：

```json
{"id": "c2d3...", "status": "completed", "resource_id": "c2d3...",
 "resource_url": "/api/audio/resources/c2d3..."}
```

`write_wave` 输出结构（R3.2）：单声道 16-bit PCM，段间静音 `int(sample_rate * pause)` 个双字节样本；文件以临时文件 + `os.replace` 原子替换。

## 关联文档与验证依据

- 上级：[服务总览](../README.md)；系统总览：[README/README.md](../../README.md)
- 同级：[runtime](../runtime/README.md)（领取与串行执行）· [conversations](../conversations/README.md)（检查点截断/永久删除时的音频清理队列）· [legacy](../legacy/README.md)（`_cleanup_audio` 删除文件）· [characters](../characters/README.md)（`voice_profile`）· [http](../http/README.md)（路由与 Range 说明）
- Agent 侧：[node/tts/README.md](../../agent/node/tts/README.md)（`create_tts_node`、`audio_sink`、无对白异常）· [config/README.md](../../config/README.md)（TTS 模型类型/音色/参考音频来自 `config/.env`）
- 测试依据：[tests/server/test_postgres.py](../../../tests/server/test_postgres.py)（语音队列与音频文件）、[tests/server/test_execution.py](../../../tests/server/test_execution.py)（受控 TTS 节点）
- 迁移自原 `server/README.md`（原文件已移除）的规则：输入由服务从消息库读取，最长 10000 字符；重复请求键返回原任务；同一消息已有排队/运行任务时复用它；失败后使用新请求键重试语音，不重新生成对话；合成对白与停顿后原子写入 `data/audio/<资源 UUID>.wav`，完成后登记资源 ID；音频只通过资源接口读取，后台不调用声卡播放；优先使用角色 `tts.md`，缺失时回退角色档案；当前只包含 TTS 和资源服务，不含 ASR；未完成语音在下一次启动标为 interrupted。
- 验证记录（迁移自原文档，未在本次编写中重跑）：语音资源验收使用受控 WAV，尚未加载真实 TTS 权重做端到端合成验收；本轮仅静态核对源码。
