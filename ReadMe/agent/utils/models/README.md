# models — 节点模型与本地推理模型工厂

## 职责与入口

- 所属类别：`agent/utils/` 支撑模块，**不是图节点**，无图注册名、无路由。
- 源码：[`agent/utils/models.py`](../../../../agent/utils/models.py)（模块 docstring：各节点共用的模型工厂；按需导入本地推理依赖并缓存实例）。
- 职责：按 `config/models.yaml` 的节点配置构建 LangChain ChatModel；按环境变量加载本地 TTS、Embedding 与 Reranker 权重；用 `lru_cache` 与配置作用域缓存避免重复初始化。
- 公开入口：`get_node_model`、`get_kimi_model`、`get_qwen_tts_model`、`get_qwen_embedding_model`、`get_reranker_model`、`load_reranker`；内部入口：`_get_default_node_model`、`_build_chat_model`、`_normalize_effort`、`_get_reranker_cross_encoder`。
- 调用方式：同步工厂函数，在节点构建阶段（ChatModel、TTS）或后台计算/脚本运行时（Embedding、Reranker）调用。函数体内部对重依赖做延迟导入，模块导入本身不加载模型。

## 调用链总览

```text
ChatModel 构建链
  B1 get_node_model(node)
       ├─ config.model_config.get_scoped_models() 为 None → B2 _get_default_node_model(node)
       │       └─ B3 _build_chat_model(node)
       └─ 作用域 dict 已存在 → 按 node 键懒建 B3 并缓存于该 dict
                B3 内部 → config.model_config.get_node_config(node)
                B3 内部 → B4 _normalize_effort(config["reasoning_effort"])
                B3 分支 → langchain_deepseek.ChatDeepSeek / langchain_moonshot.ChatMoonshot
                          / langchain_community.chat_models.ChatLlamaCpp

本地模型加载链（均为 lru_cache 单例）
  B5 get_kimi_model()                     ← 固定 Moonshot kimi-k3（当前仓库无调用方）
  B6 get_qwen_tts_model()                 ← qwen_tts.Qwen3TTSModel，按 TTS_MODEL_TYPE 选权重
  B7 get_qwen_embedding_model()           ← sentence_transformers.SentenceTransformer
  B8 get_reranker_model(top_k=15)         ← B8.1 _get_reranker_cross_encoder + CrossEncoderReranker
  B9 load_reranker(model_path, top_k=4)   ← 独立加载，不缓存（当前仓库无调用方）

调用方（节点名 → 消费函数）
  main                → agent/node/draft.py:50
  check               → agent/node/check.py:150
  participant_state   → agent/node/participant_state.py:56
  tts                 → agent/node/tts.py:113（语气指令）；get_qwen_tts_model → agent/node/tts.py:158
  chunking            → agent/utils/chunking.py:106
  memory_query        → agent/memory/processor.py:178
  memory_summary      → agent/memory/processor.py:197
  get_qwen_embedding_model → agent/memory/store.py:153、agent/memory/processor.py:58、
                             agent/tools/memory_query.py:62、scripts/rebuild_character_memory.py:106
  get_reranker_model  → agent/memory/store.py:309（search_hybrid 精排）
```

## 构建链

### B1. `get_node_model`

- 定位与签名：`get_node_model(node: str)`，[`agent/utils/models.py:36`](../../../../agent/utils/models.py)，同步函数。
- 调用方与条件：各节点构建或后台计算时按节点名调用，见上表；同一节点名在同一个配置作用域内重复调用返回同一实例。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `node` | `str` | 必填 | `config/models.yaml` 的 `nodes` 键：`main`、`check`、`participant_state`、`tts`、`chunking`、`memory_query`、`memory_summary` |

隐式输入：

- `config.model_config.get_scoped_models()` 返回的 ContextVar dict；服务任务由 `server/services/agent.py` 的 `model_config_scope(data)` 在 `execute` 内设置（[`server/services/agent.py:133`](../../../../server/services/agent.py)、[`config/model_config.py:17`](../../../../config/model_config.py)）。
- 无作用域时使用模块级 `lru_cache(maxsize=None)` 的 `_get_default_node_model`（CLI/脚本路径）。

功能与内部调用：

1. 读取当前作用域模型 dict。
2. `None`（CLI/脚本）→ 调 B2 取进程级缓存实例。
3. 非 `None`（服务任务）→ 若 `node` 不在 dict 中，调 B3 构建并写入 `models[node]`；返回 `models[node]`。这样并发任务的事件循环各自持有独立实例，互不清理。
4. 模块底部把 `_get_default_node_model.cache_clear` 暴露为 `get_node_model.cache_clear`（[`agent/utils/models.py:47`](../../../../agent/utils/models.py)），当前仓库内无调用方；它只清默认缓存，不影响作用域 dict。

输出：LangChain ChatModel 实例（`ChatDeepSeek` / `ChatMoonshot` / `ChatLlamaCpp`）。

副作用：首次构建时导入第三方提供方包并创建客户端对象；作用域 dict 被原地写入。

异常与边界：未知节点在 B3 的 `get_node_config` 处抛 `KeyError`（列出可用节点）；未知 provider 抛 `ValueError`。缓存键只含 `node`，不含配置内容——配置变化需要新的 `model_config_scope` 或 `cache_clear`。

后续去向：返回调用方后通常再 `.bind_tools(...)`（draft、processor）或 `.with_structured_output(...)`（check），不修改本模块缓存对象。

### B2. `_get_default_node_model`

- 定位与签名：`_get_default_node_model(node: str)`，[`agent/utils/models.py:31`](../../../../agent/utils/models.py)，同步内部函数，`@lru_cache(maxsize=None)`。
- 调用方与条件：仅 B1 在无配置作用域时调用。

输入：`node`（`str`）。隐式输入：模块级 lru 缓存。

功能与内部调用：直接 `return _build_chat_model(node)`（B3），由 `lru_cache` 按 `node` 记住实例。

输出：进程内长期存活的 ChatModel 实例（每个节点一个）。

副作用：首次调用触发 B3 的导入与网络客户端创建。

异常与边界：构建异常不会进入缓存，下次调用会重试。

后续去向：返回 B1。

### B3. `_build_chat_model`

- 定位与签名：`_build_chat_model(node: str)`，[`agent/utils/models.py:121`](../../../../agent/utils/models.py)，同步内部函数。
- 调用方与条件：B1（作用域缺键时）与 B2（默认缓存未命中时）。

| 输入参数或配置项 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `node` | `str` | 调用方 | 必填 | 取节点配置 |
| `provider` | `str` | `get_node_config(node)` | 默认 `"deepseek"`，先 `strip().lower()` | 选择提供方分支 |
| `thinking` | `str` | 节点配置 | 默认 `"enabled"`；等于 `"enabled"` 时为思考模式 | deepseek 的 `extra_body.thinking` 与 `reasoning_effort` |
| `reasoning_effort` | `str | None` | 节点配置 | 经 B4 归一 | 思考强度 |
| `model` / `base_url` / `model_path` / `n_ctx` / `temperature` | 各自类型 | 节点配置 | 见分支说明 | 提供方参数 |

隐式输入：`config/models.yaml` 的 `defaults`（`provider: deepseek`、`thinking: enabled`、`reasoning_effort: max`）与节点覆盖；`get_node_config` 做浅合并（[`config/model_config.py:44`](../../../../config/model_config.py)）。

功能与内部调用：

1. `config = get_node_config(node)`；解析 `provider`、`thinking`、`effort = _normalize_effort(config.get("reasoning_effort"))`。
2. `provider == "deepseek"`：延迟导入 `langchain_deepseek.ChatDeepSeek`；kwargs 固定 `model=config["model"]` 与 `extra_body={"thinking": {"type": "enabled"|"disabled"}}`；当 `thinking` 为真时追加 `reasoning_effort=effort or "max"` 与 `disabled_params={"tool_choice": None}`（注释说明：思考模式不支持强制 `tool_choice`，结构化输出只能由模型自行选择工具）。
3. `provider == "moonshot"`：延迟导入 `langchain_moonshot.ChatMoonshot`，`base_url` 默认 `https://api.moonshot.cn/v1`，`model=config["model"]`，`reasoning_effort=effort or "max"`。
4. `provider == "llama_cpp"`：延迟导入 `langchain_community.chat_models.ChatLlamaCpp`，`model_path=config["model_path"]`、`n_ctx=config.get("n_ctx", 4096)`、`temperature=config.get("temperature")`。
5. 其他 provider 抛 `ValueError(f"[ModelConfig] 未知 provider: {provider}")`。

输出：对应提供方的 LangChain ChatModel 实例。

副作用：导入提供方包并构造客户端；本模块不显式读取 API key，凭证由各提供方包自行处理（`config/config.py` 中定义了 `DEEPSEEK_API_KEY` / `KIMI_API_KEY` 供环境使用）。

异常与边界：节点未配置 `model`（deepseek/moonshot）或 `model_path`（llama_cpp）时抛 `KeyError`；未知 provider 抛 `ValueError`；构建失败不写缓存。

后续去向：由 B1/B2 缓存并返回。

### B4. `_normalize_effort`

- 定位与签名：`_normalize_effort(effort) -> str | None`，[`agent/utils/models.py:113`](../../../../agent/utils/models.py)，同步内部函数。
- 调用方与条件：B3 无条件调用。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `effort` | 任意 / `None` | 必填 | 节点配置的 `reasoning_effort` |

功能与内部调用：`None` 返回 `None`；`str(effort).strip().lower() == "none"` 返回 `None`；否则返回 `str(effort)`（保留原始大小写）。

输出：`str | None`。副作用：无。异常与边界：任意可 `str()` 的值都能处理；`"none"` 与 `None` 等价。

后续去向：返回 B3 用于 `effort or "max"` 回退。

### B5. `get_kimi_model`

- 定位与签名：`get_kimi_model()`，[`agent/utils/models.py:20`](../../../../agent/utils/models.py)，`@lru_cache(maxsize=1)`。
- 调用方与条件：**当前仓库内无调用方**。根目录 `temp_llm.py` 的 `from agent.models import get_kimi_model` 指向不存在的旧路径，属于遗留草稿，不是有效消费者。

功能与内部调用：延迟导入 `langchain_moonshot.ChatMoonshot`，固定 `base_url='https://api.moonshot.cn/v1'`、`model="kimi-k3"`、`reasoning_effort="max"`。

输出：全局唯一的 `ChatMoonshot` 实例。副作用：导入包并创建客户端。异常与边界：未配置 `KIMI_API_KEY` 时的报错由提供方包在调用时决定。

后续去向：无运行调用方，标注为备用工厂。

### B6. `get_qwen_tts_model`

- 定位与签名：`get_qwen_tts_model()`，[`agent/utils/models.py:49`](../../../../agent/utils/models.py)，`@lru_cache(maxsize=1)`。
- 调用方与条件：`agent/node/tts.py` 的 `_ensure_model` 在首次需要合成时调用（[`agent/node/tts.py:158`](../../../../agent/node/tts.py)）。

隐式输入（环境变量，全部来自 `config/config.py`）：

- `TTS_MODEL_TYPE`：`voice_design`（默认）| `custom_voice` | `base`；
- `Qwen3_TTS_12Hz_1_7B-VoiceDesign` / `Qwen3_TTS_12Hz_1_7B-Custom` / `Qwen3_TTS_12Hz_1_7B-Base`：对应权重路径。

功能与内部调用：

1. 延迟导入 `torch` 与 `qwen_tts.Qwen3TTSModel`。
2. `model_type = TTS_MODEL_TYPE.strip().lower()`；按 `path_map` 取路径；未知类型抛 `ValueError`（列出可选值）。
3. 路径为空或不存在抛 `FileNotFoundError`。
4. `torch.cuda.is_available()` 时用 `device_map="cuda"` 与 `dtype=torch.bfloat16`，否则不传 kwargs。
5. `Qwen3TTSModel.from_pretrained(model_path, **kwargs)`，记录日志后返回。

输出：全局唯一的 Qwen3TTSModel 实例（权重只加载一次）。

副作用：加载大模型权重到显存/内存；写日志。

异常与边界：未知 `TTS_MODEL_TYPE`、路径缺失、权重加载失败均向上抛出；`lru_cache` 不缓存异常，重试会重新加载。调用方 `agent/node/tts.py` 在节点构建阶段只校验 `TTS_MODEL_TYPE` 合法性，真正的模型加载延迟到首次合成。

后续去向：`tts` 节点用其 `generate_voice_design` / `generate_custom_voice` / `generate_voice_clone`（由 `TTS_MODEL_TYPE` 决定）。

### B7. `get_qwen_embedding_model`

- 定位与签名：`get_qwen_embedding_model()`，[`agent/utils/models.py:77`](../../../../agent/utils/models.py)，`@lru_cache(maxsize=1)`。
- 调用方与条件：`agent/memory/store.py` 的 `prepare_memory`（编码子块）、`agent/memory/processor.py` 的 `_retrieve_memories`（编码查询）、`agent/tools/memory_query.py` 的 `_arun`、`scripts/rebuild_character_memory.py`。

隐式输入：`QWEN3_EMBEDDING_PATH` 环境变量。

功能与内部调用：延迟导入 `sentence_transformers.SentenceTransformer`；路径为空或不存在抛 `FileNotFoundError`；`SentenceTransformer(QWEN3_EMBEDDING_PATH)` 后记录日志返回。

输出：全局唯一的 `SentenceTransformer` 实例。

副作用：加载权重；日志。

异常与边界：路径缺失抛 `FileNotFoundError`；加载失败向上抛出且不缓存。`store.prepare_memory` 与 `processor._retrieve_memories` 在 `asyncio.to_thread` 中调用它，避免阻塞事件循环。

后续去向：调用方用 `encoder.encode(text)`（查询侧带 `prompt_name="query"`）得到向量。

### B8. `get_reranker_model` 与 B8.1 `_get_reranker_cross_encoder`

- 定位与签名：`get_reranker_model(top_k: int = 15)`，[`agent/utils/models.py:106`](../../../../agent/utils/models.py)，`@lru_cache(maxsize=8)`；内部 `_get_reranker_cross_encoder()`，[`agent/utils/models.py:90`](../../../../agent/utils/models.py)，`@lru_cache(maxsize=1)`。
- 调用方与条件：`agent/memory/store.py` 的 `_get_reranker`（实例级薄缓存）在 `search_hybrid` 精排时调用（[`agent/memory/store.py:305`](../../../../agent/memory/store.py)）。

隐式输入：`BGEV2M3_RERANKER_PATH` 环境变量。

功能与内部调用：

1. B8 延迟导入 `agent.classes.reranker.CrossEncoderReranker`；
2. 调 B8.1 取共享交叉编码器：路径为空/不存在抛 `FileNotFoundError`；`HuggingFaceCrossEncoder(model_name=..., model_kwargs={"device": "cuda"})`；日志；
3. 用 `CrossEncoderReranker(model=<共享编码器>, top_k=top_k)` 包装并返回；`lru_cache(maxsize=8)` 按 `top_k` 缓存最多 8 个包装器，权重只加载一次。

输出：`CrossEncoderReranker` 实例（`top_k` 为最终返回条数上限）。

副作用：首次调用加载权重到 CUDA；日志。

异常与边界：路径缺失抛 `FileNotFoundError`；`model_kwargs` 固定 `device="cuda"`，无 CUDA 环境时由 `HuggingFaceCrossEncoder` 决定报错方式。`store._rerank` 捕获精排异常并回退粗排结果。

后续去向：`store._rerank` 调用 `reranker.acompress_documents(docs, query_text)`。

### B9. `load_reranker`

- 定位与签名：`load_reranker(model_path: str = BGEV2M3_RERANKER_PATH, top_k: int = 4)`，[`agent/utils/models.py:164`](../../../../agent/utils/models.py)，同步函数。
- 调用方与条件：**当前仓库内无调用方**，标注为备用/独立加载入口。

功能与内部调用：路径为空/不存在抛 `FileNotFoundError`；延迟导入 `torch`、`CrossEncoderReranker`、`HuggingFaceCrossEncoder`；用 `model_kwargs={"device": "cuda", "torch_dtype": torch.float16}` 构造编码器，包装 `top_k` 返回。

输出：新的 `CrossEncoderReranker` 实例（不缓存，每次调用重新加载权重）。

副作用：每次调用都加载权重。异常与边界：路径缺失抛 `FileNotFoundError`。后续去向：无运行调用方。

## 分支与异常链

| 条件 | 行为 | 终点 |
| --- | --- | --- |
| 服务任务在 `model_config_scope` 内 | B1 使用作用域 dict，任务间互不共享模型 | 任务结束随 ContextVar 重置回收 |
| CLI/脚本无作用域 | B1 使用进程级 lru 缓存 | 进程生命周期内复用 |
| 未知 `node` | `get_node_config` 抛 `KeyError` | 调用方构建失败 |
| 未知 `provider` | B3 抛 `ValueError` | 调用方构建失败 |
| `thinking: disabled` | deepseek 不传 `reasoning_effort` 与 `disabled_params` | 模型按提供方默认行为 |
| `reasoning_effort: none` | B4 归一为 `None`，各分支回退 `"max"` | 不显式关闭思考强度字段 |
| `TTS_MODEL_TYPE` 未知 | B6 抛 `ValueError` | `tts` 节点首次合成失败 |
| 本地权重路径缺失 | B6/B7/B8.1/B9 抛 `FileNotFoundError` | 调用方失败或回退（精排回退粗排） |

## 输入输出示例

适用 B1/B3（配置来自 `config/models.yaml`）：

```text
get_node_model("chunking")
  → get_node_config("chunking") 合并 defaults 后：
      provider=deepseek, thinking=enabled, reasoning_effort=low, model=deepseek-v4-pro
  → ChatDeepSeek(model="deepseek-v4-pro",
                 extra_body={"thinking": {"type": "enabled"}},
                 reasoning_effort="low",
                 disabled_params={"tool_choice": None})
```

适用 B6（环境变量示意）：

```text
TTS_MODEL_TYPE=voice_design
Qwen3_TTS_12Hz_1_7B-VoiceDesign=D:\models\Qwen3-TTS-12Hz-1.7B-VoiceDesign
→ Qwen3TTSModel.from_pretrained(该路径, device_map="cuda", dtype=torch.bfloat16)  # CUDA 可用时
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)（agent/utils 总览）
- 配置依据：[`config/models.yaml`](../../../../config/models.yaml)、[`config/model_config.py`](../../../../config/model_config.py)、[`config/config.py`](../../../../config/config.py)
- 相关模块：[`../chunking/README.md`](../chunking/README.md)（`chunking` 节点模型消费）、[`../../classes/README.md`](../../classes/README.md)（`CrossEncoderReranker` 协议）、[`../../memory/store/README.md`](../../memory/store/README.md)（Embedding 与 Reranker 消费）
- 已有测试覆盖（本次未执行）：`tests/test_model_scopes.py`（并发作用域隔离、嵌套作用域恢复、`cache_clear` 语义的替代验证）、`tests/test_module_layout.py`（离线导入扫描）
- 验证情况：本页为静态阅读源码所得；`get_kimi_model` 与 `load_reranker` 无运行调用方，`temp_llm.py` 引用的是旧路径 `agent.models`，不作为消费依据。本次文档编写未实际执行测试。
