# agent/utils — Agent 共享工具与支撑模块

`agent/utils/` 是 Agent 的共享能力层，为图节点、后台记忆服务和脚本提供可复用的基础函数：模型实例构建与缓存、世界/角色/用户状态格式化、消息快照与安全裁剪、记忆分块、Token 估算、文本清洗、角色档案读取、语言归一化和实时天气客户端。本目录中的模块都不是图节点，不在 `agent/builder.py` 中注册，也不参与 LangGraph 调度；它们由节点、`agent/memory/*` 与 `scripts/*` 在函数级调用。`__init__.py` 仅含模块说明，无运行时代码。

## 子目录与节点

| 名称 | 类型 | 主要功能 | 协作对象 | 源码 | 文档 |
| --- | --- | --- | --- | --- | --- |
| `character` | 支撑模块（角色档案） | 定位 `Character/<角色名>` 目录；按语言读取角色档案，缺失时回退到任一 `profile_*.md` | `agent/builder.py`、`agent/prompts/main/draft.py`、`agent/node/tts.py` | [`character.py`](../../../agent/utils/character.py) | [`character/README.md`](character/README.md) |
| `chunking` | 支撑模块（记忆分块） | LLM 语义分块优先，失败回退正则断句；提取关键词与事件日期 | `agent/memory/store.py` 的 `prepare_memory`；`scripts/rebuild_character_memory.py` | [`chunking.py`](../../../agent/utils/chunking.py) | [`chunking/README.md`](chunking/README.md) |
| `context` | 支撑模块（Token 估算） | 按 UTF-8 字节估算文本与消息（含工具调用参数）的 token 数 | `agent/node/context.py` 的 `limit_context`、`agent/node/event.py` 的 `event_judge` | [`context.py`](../../../agent/utils/context.py) | [`context/README.md`](context/README.md) |
| `language` | 支撑模块（语言归一化） | 把任意语言标识归一为 `zh` / `en` | `agent/utils/character.py` 与各带语言分支的 prompt 模块 | [`language.py`](../../../agent/utils/language.py) | [`language/README.md`](language/README.md) |
| `memory` | 支撑模块（记忆快照与裁剪） | 冻结消息快照、计算消息指纹、构建后台任务载荷、生成安全的 `RemoveMessage` 裁剪列表 | `agent/node/memory.py`、`agent/memory/processor.py`、`agent/memory/jobs.py` | [`memory.py`](../../../agent/utils/memory.py) | [`memory/README.md`](memory/README.md) |
| `messages` | 支撑模块（历史序列化） | 把消息列表格式化为 `<history_messages>` 文本，可选保留工具调用标记 | `agent/memory/processor.py` | [`messages.py`](../../../agent/utils/messages.py) | [`messages/README.md`](messages/README.md) |
| `models` | 支撑模块（模型工厂） | 构建并缓存节点 ChatModel、TTS、Embedding 与 Reranker | 各模型调用节点、`agent/memory/store.py`、`agent/memory/processor.py`、`scripts/rebuild_character_memory.py` | [`models.py`](../../../agent/utils/models.py) | [`models/README.md`](models/README.md) |
| `state` | 支撑模块（状态格式化） | 用系统时间生成世界状态，格式化世界/角色/用户状态文本 | `agent/node/world_state.py`、`agent/node/participant_state.py`、`agent/node/draft.py`、`agent/memory/processor.py` | [`state.py`](../../../agent/utils/state.py) | [`state/README.md`](state/README.md) |
| `text` | 支撑模块（文本清洗） | 提取消息文本块、移除时间戳、剥离 Markdown 代码围栏 | `agent/node/draft.py`、`agent/node/check.py`、`agent/node/participant_state.py`、`agent/node/tts.py`、`agent/utils/chunking.py`、`agent/memory/processor.py` | [`text.py`](../../../agent/utils/text.py) | [`text/README.md`](text/README.md) |
| `weather` | 支撑模块（外部服务客户端） | 和风天气实时天气：Ed25519 JWT 认证、结果缓存、失败返回 `None` | `agent/node/world_state.py` | [`weather.py`](../../../agent/utils/weather.py) | [`weather/README.md`](weather/README.md) |

## 整体流程

本目录不承载图调度，按“谁在什么阶段调用哪个共享函数”表达协作关系：

```text
构建阶段
  builder.build_rp_agent
    └─ character.load_character_profile ──────────→ create_draft_node 的 character_profile

图内运行阶段（调用方为 builder 注册的节点）
  world_state_update ── state.prepare_world_state + weather.fetch_current_weather
  participant_state_in / participant_state_out
      ── state.prepare_{world,character,user}_state
      ── text.{content_text, strip_timestamps, strip_code_fence}
      ── models.get_node_model("participant_state")
  draft ── state.prepare_{world,character,user}_state
      ── text.{content_text, strip_timestamps}
      ── models.get_node_model("main")（角色档案经 prompts.main.draft 读取）
  check ── text.{content_text, strip_timestamps} + models.get_node_model("check")
  limit_context ── context.estimate_message_tokens
  event_judge ── context.estimate_message_tokens / estimate_tokens_from_bytes
  prepare_memory ── memory.build_memory_payload（内部先 freeze_messages）
  enqueue_memory ── 把 memory payload 交给 agent/memory/jobs.enqueue（队列本身不在本目录）
  apply_memory_results ── memory.{freeze_messages, message_fingerprint, result_removals}
  tts ── models.{get_node_model, get_qwen_tts_model}
      ── text.{strip_timestamps, strip_code_fence} + character.character_dir

后台记忆服务与脚本（独立 Worker 进程或脚本进程）
  agent/memory/processor ── messages.format_history + memory.history_removals
      ── state.prepare_world_state + models.{get_node_model, get_qwen_embedding_model}
  agent/memory/store ── chunking.chunk_memory
      ── models.{get_qwen_embedding_model, get_reranker_model}
  agent/memory/jobs ── memory.message_fingerprint
  scripts/rebuild_character_memory ── chunking.{chunk_memory, split_memory_text,
      extract_keywords, extract_event_date} + models.get_qwen_embedding_model
```

`language.normalize_language` 是上述链条之外的公共前置函数：`character` 与所有带 `language` 参数的 prompt 模块在取提示词/档案前先调用它（见 [`language/README.md`](language/README.md)）。

## 主要数据与依赖

- **配置来源**：`config/config.py` 提供 `PROJECT_ROOT`、`MODEL_CONTEXT_TOKEN_BUDGET`、`QWEN3_EMBEDDING_PATH`、`BGEV2M3_RERANKER_PATH`、`Qwen3_TTS_12Hz_1_7B-{Base,Custom,VoiceDesign}`、`TTS_MODEL_TYPE`、`TTS_SPEAKER_ID`、`TTS_VOICE_REFERENCE_PATH`、`QWEATHER_*` 等环境变量；`config/model_config.py` 提供 `get_node_config` 与 `get_scoped_models`，服务任务通过 `model_config_scope` 使用独立配置快照和节点模型缓存。
- **跨模块数据**：世界/角色/用户状态文本由节点拼接进系统提示词；记忆快照（`memory.build_memory_payload` 产出的 JSON）经 `memory_pending_job` 入队，后台处理结果再经 `memory.result_removals` 回到图内；分块结果与 Embedding 由 `agent/memory/store.py` 写入 PostgreSQL。
- **外部依赖**：LangChain ChatModel 提供方（deepseek / moonshot / llama_cpp）、`sentence_transformers`、`langchain_community` 的 `HuggingFaceCrossEncoder`、`qwen_tts`、`torch`、`requests`、`PyJWT`（EdDSA）。
- **边界**：本目录不读写数据库、不注册图节点。唯一的网络请求是 `weather.fetch_current_weather`；唯一的文件读取是角色档案（`character.py`）与天气私钥（`weather.py`）。

## 阅读导航

- 上级：[`../README.md`](../README.md)（Agent 总览）
- 子模块：[`character/README.md`](character/README.md)、[`chunking/README.md`](chunking/README.md)、[`context/README.md`](context/README.md)、[`language/README.md`](language/README.md)、[`memory/README.md`](memory/README.md)、[`messages/README.md`](messages/README.md)、[`models/README.md`](models/README.md)、[`state/README.md`](state/README.md)、[`text/README.md`](text/README.md)、[`weather/README.md`](weather/README.md)
- 协作文档：[`../classes/README.md`](../classes/README.md)（状态与协议）、[`../prompts/README.md`](../prompts/README.md)（提示词注册与消费）、[`../memory/README.md`](../memory/README.md)（记忆服务整体设计）
