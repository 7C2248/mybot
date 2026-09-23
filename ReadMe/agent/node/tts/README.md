# tts 节点（语音处理）

## 职责与入口

- 所属类别：图节点工厂。
- 源码：[agent/node/tts.py](../../../../agent/node/tts.py)
- 图注册名：`tts` → `create_tts_node(character_name=...)` 返回的 `tss_node`（[agent/builder.py](../../../../agent/builder.py)）。
- 上游/下游：`update_iter` 固定边到 `tts`；`tts` 的条件边由 `event_judge` 决定去 `prepare_memory` 或 `END`。
- 触发时机：每轮回复提交后；`need_tts=False` 时直接跳过。
- 其他使用方：`server/services/speech.py` 直接调用 `create_tts_node(..., audio_sink=...)` 生成 WAV 资源，不经过图。

## 调用链总览

| 阶段 | 步骤 | 关系 |
| --- | --- | --- |
| 构建 | B1 `create_tts_node` | 工厂：校验 TTS 类型、加载语音档案、建立模型缓存 |
| 构建 | B1.1 `_load_voice_profile` | 读取角色语音档案 |
| 运行 | R1 `tss_node(state)` | 图调度（或 `SpeechService.execute` 直接调用） |
| 运行 | R1.1 `_parse_reply` | 解析对白与动作 |
| 运行 | R1.2 `_generate_segment_plans` → `_parse_plans` | 生成语气指令与停顿 |
| 运行 | R1.3 `_ensure_model` → `synthesize` → `synthesize_and_play` | 延迟加载模型、合成、播放或回调 |

## 构建链

### B1. `create_tts_node`

- 定位与签名：`create_tts_node(character_name: str, *, audio_sink=None, character_file: str | None = None)`，同步工厂，返回 `async def tts_node(state)`，[agent/node/tts.py:146](../../../../agent/node/tts.py#L146)。
- 调用方与条件：`build_rp_agent` 注册 `tts` 时调用；`SpeechService` 传入 `audio_sink` 复用同一工厂。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `character_name` | `str` | 必填 | 用于查找角色语音档案 |
| `audio_sink` | 可调用或 `None` | 默认 `None` | 音频输出回调 `(waveforms, sample_rate, pauses)`；服务端传入写 WAV 的实现 |
| `character_file` | `str` 或 `None` | 默认 `None` | 语音档案文本；`None` 时读取角色目录 |

功能与内部调用：

1. 读取 `TTS_MODEL_TYPE`（默认 `voice_design`），只允许 `voice_design`/`custom_voice`/`base`，否则抛 `ValueError`。
2. `character_file` 未提供时调用 `_load_voice_profile(character_name)`（B1.1）。
3. 建立闭包缓存 `{"model": None, "speaker": None, "prompt_items": None}`，模型权重延迟到首次合成时加载。

输出：执行函数 `tts_node`。副作用：构建期读取角色文件；不加载模型。

### B1.1. `_load_voice_profile`

- 定位与签名：`_load_voice_profile(character_name: str) -> str`，同步私有函数，[agent/node/tts.py:62](../../../../agent/node/tts.py#L62)。
- 读取顺序：`Character/<角色>/tts.md` → `profile_cn.md` → `profile_en.md` → 排序后的第一个 `profile_*.md` → `""`。
- 输出：语音提示词使用的档案文本；缺失时不报错。

## 运行链

### R1. `tts_node(state)`

- 定位与签名：`create_tts_node.<locals>.tts_node(state)`，异步函数，[agent/node/tts.py:194](../../../../agent/node/tts.py#L194)。
- 调用方与条件：图调度（每轮）或 `SpeechService.execute`（独立任务）。图输入由服务端固定为 `need_tts=False`，因此服务模式的语音由图外任务产生；图内播放路径用于本地/测试场景。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `need_tts` | `bool` | 初始输入 | 缺省 `False` | 关闭时直接返回 `{}` |
| `messages` | `list` | checkpoint | 最后一条必须是 `AIMessage` 才处理 | 取最后一条正式回复正文 |

隐式输入：闭包中的 `character_file`、`audio_sink`、模型缓存；`get_node_model("tts")`、`get_qwen_tts_model()`、`config.config` 的 `TTS_MODEL_TYPE`/`TTS_SPEAKER_ID`/`TTS_VOICE_REF_PATH`；常量 `_DEFAULT_PAUSE=0.3`、`_MAX_PAUSE=3.0`。

功能与内部调用：

1. `need_tts` 为假返回 `{}`；`messages` 为空或最后一条不是 `AIMessage` 返回 `{}`。
2. `segments = _parse_reply(content)`（R1.1），过滤出有文本的对白；`texts` 为空时：
   - `audio_sink is not None`（服务端合成任务）抛 `ValueError("speech_empty")`，让任务明确失败；
   - 否则记录“无对白，跳过语音生成”并返回 `{}`。
3. `plans = await _generate_segment_plans(segments, character_file)`（R1.2）；失败时记录 warning 并用默认计划补齐（每段 `instruct=""`、`pause=0.3`）。
4. 拆分 `instructions` 与 `pauses`，记录合成日志。
5. `await asyncio.to_thread(synthesize_and_play, texts, instructions, pauses)`（R1.3）：在线程中加载模型、合成并交给 `audio_sink` 或 `_play_audio`。
   - 异常处理：`audio_sink` 存在时原样抛出（任务失败）；否则记录 warning 并继续。
6. 返回 `{}`（语音不产生图状态增量）。

### R1.1. `_parse_reply`

- 定位与签名：`_parse_reply(content: str) -> list[dict]`，同步私有函数，[agent/node/tts.py:29](../../../../agent/node/tts.py#L29)。
- 规则（与主节点正文格式对应）：
  - 先 `strip_timestamps`；逐行处理；
  - 删除 `「...」` 设备消息（不朗读）；
  - 提取 `（...）` 动作/旁白作为 `description`（不朗读）；
  - 其余文本视为对白，删除 `♡` 后作为 `text`；
  - 纯动作行（无对白）的描述并入下一段对白。
- 输出：`[{"text": str, "description": str}]`；空行跳过。

### R1.2. `_generate_segment_plans` 与 `_parse_plans`

- `_generate_segment_plans(segments, character_file)`：[agent/node/tts.py:111](../../../../agent/node/tts.py#L111)。调用 `get_node_model("tts")`，system 为 `get_prompt("tts_instruct", character_file=...)`，user 为编号的“对白 | 描述”列表；响应正文为空时尝试从 `reasoning_content` 的 `</thinking_process>` 之后取文本；交给 `_parse_plans`。
- `_parse_plans(content, count)`：[agent/node/tts.py:81](../../../../agent/node/tts.py#L81)。`strip_code_fence` 后 `json.loads`；解析失败或结构不符时返回全默认计划；每项取 `instruct`（去空白）与 `pause`（float，钳制到 `[0, 3]`）；不足 `count` 补齐，超出截断。
- 输出：与对白数量对齐的 `[{"instruct": str, "pause": float}]`。

### R1.3. `_ensure_model` / `synthesize` / `synthesize_and_play`

- `_ensure_model`：[agent/node/tts.py:155](../../../../agent/node/tts.py#L155)。首次调用时 `get_qwen_tts_model()` 加载权重；`custom_voice` 选择 `TTS_SPEAKER_ID` 或首个支持音色（无可用音色抛 `ValueError`）；`base` 用 `TTS_VOICE_REF_PATH` 创建 `x_vector_only_mode` 声音克隆提示。
- `synthesize(texts, instructions)`：按类型调用 `generate_voice_design` / `generate_custom_voice` / `generate_voice_clone`，`language="Auto"`，返回 `(waveforms, sample_rate)`。
- `synthesize_and_play(texts, instructions, pauses)`：`audio_sink` 存在时交给它；否则调用 `_play_audio`（`sounddevice` 逐段播放，段间 `time.sleep(pause)`）。
- 异常与边界：模型路径缺失由 `get_qwen_tts_model` 抛 `FileNotFoundError`；播放失败在无 `audio_sink` 时只记录 warning；原生推理不可安全中断。

| 输出或状态字段 | 类型 | 产生条件 | 含义 | 消费方 |
| --- | --- | --- | --- | --- |
| （无状态增量） | `{}` | 总是 | 语音不修改图状态 | `event_judge` 条件边 |

副作用：模型权重加载、音频合成、声卡播放或 WAV 写入（由 `audio_sink` 决定）。

后续去向：条件边 `event_judge` 决定进入 `prepare_memory` 或 `END`。

## 分支与异常链

- **`need_tts=False`**：整节点跳过，图继续走 `event_judge`。
- **回复无对白**：服务端任务抛 `speech_empty` 并标记语音失败；图内路径跳过。
- **指令生成失败**：使用默认语气与停顿继续合成。
- **合成/播放失败**：服务端任务失败（可单独重试语音）；图内路径降级为 warning。
- **模型类型未知/权重缺失**：构建期或首次合成时抛错。

## 输入输出示例

适用 R1（图内路径）：

```text
输入：need_tts=True，messages[-1].content =
      "<timestamp>...</timestamp>\n「好的。」\n（我点点头。）\n先这样吧。"
解析：segments=[{"text":"先这样吧。","description":"我点点头。"}]（设备消息被丢弃）
计划：plans=[{"instruct":"年轻女性，清脆，平静","pause":0.3}]
输出：{}
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 路由：[../event/README.md](../event/README.md) · 提示词：[../../prompts/tools/tts_instruct/README.md](../../prompts/tools/tts_instruct/README.md)
- 模型工厂：[../../utils/models/README.md](../../utils/models/README.md) · 服务语音：[server/services/speech.py](../../../../server/services/speech.py)
- 依据：`agent/node/tts.py`；`server/services/speech.py` 复用 `audio_sink`；真实 TTS 权重端到端合成尚未在文档编写时验证。
