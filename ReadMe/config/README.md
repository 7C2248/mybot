# config（运行配置与节点模型配置）

`config/` 负责加载本地 `.env`、暴露运行配置，并把 `models.yaml` 合并为各节点的模型配置。它被 Agent、服务端、CLI 与维护脚本共同使用。

## 模块

| 名称 | 类型 | 主要功能 | 源码 |
| --- | --- | --- | --- |
| `config/config.py` | 配置常量 | 路径、API key、数据库、天气、模型路径、TTS、记忆阈值 | [config.py](../../config/config.py) |
| `config/model_config.py` | 模型配置 | 读取 `models.yaml`、作用域快照与节点配置合并 | [model_config.py](../../config/model_config.py) |
| `config/models.yaml` | 运行配置 | 当前节点模型设置（本地文件） | [models.yaml](../../config/models.yaml) |
| `config/models_example.yaml` | 示例 | 节点模型配置模板 | [models_example.yaml](../../config/models_example.yaml) |
| `config/.env_example` | 示例 | 环境变量模板（复制为 `.env`） | [.env_example](../../config/.env_example) |

## 配置项

### `config/config.py`

- 加载：`load_dotenv(config/.env, verbose=True)`；`PROJECT_ROOT`、`CONFIG_DIR`、`DATA_DIR` 在导入时计算。
- 主要环境变量：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` / `KIMI_API_KEY` | 空 | 模型 API 凭据 |
| `DB_URL` | 空 | Postgres 连接串 |
| `MODEL_CONTEXT_TOKEN_BUDGET` | `16000`（最小 2048） | 模型上下文 token 预算（[../agent/node/context/README.md](../agent/node/context/README.md)） |
| `MINIMUM_ITERATIONS` / `MAXIMUM_ITERATIONS` / `MEMORY_TOKEN_THRESHOLD` | `3` / `20` / `20000` | 记忆处理轮数/token 阈值（[../agent/node/event/README.md](../agent/node/event/README.md)） |
| `QWEATHER_*` | 空/`zh`/`1200` | 和风天气 JWT 与缓存（[../agent/utils/weather/README.md](../agent/utils/weather/README.md)） |
| `QWEN3_EMBEDDING_PATH`、`BGEV2M3_RERANKER_PATH` 等 | 空 | 本地模型路径 |
| `Qwen3_TTS_12Hz_1_7B-*`、`TTS_MODEL_TYPE`、`TTS_SPEAKER_ID`、`TTS_VOICE_REFERENCE_PATH` | `voice_design` 等 | TTS 类型与权重（[../agent/node/tts/README.md](../agent/node/tts/README.md)） |
| `DEFAULT_DOWNLOAD_DIR` | `data/downloads` | 脚本下载目录 |

### `config/model_config.py`

| 函数 | 签名 | 行为 |
| --- | --- | --- |
| `model_config_scope` | `(data: dict)` 上下文管理器 | 用 `ContextVar` 设置当前配置快照并新建独立模型缓存；退出时还原（服务任务隔离配置与模型实例） |
| `get_scoped_models` | `() -> dict \| None` | 返回当前执行上下文的模型缓存；无快照返回 `None` |
| `get_node_config` | `(node: str) -> dict` | 有活动快照时用它，否则读取 `models.yaml`；把 `defaults` 与 `nodes[node]` 合并；未知节点抛 `KeyError` |
| `_load_model_config` | `() -> dict` | `lru_cache` 读取 YAML；文件缺失抛 `FileNotFoundError`，缺少 `nodes` 抛 `ValueError` |

- 节点名：`main`、`check`、`participant_state`、`event_judge`、`memory_query`、`memory_summary`、`chunking`、`tts`。
- 字段：`provider`（`deepseek`/`moonshot`/`llama_cpp`）、`model`、`thinking`、`reasoning_effort`；具体构建见 [../agent/utils/models/README.md](../agent/utils/models/README.md)。
- 服务端保存/生效：`ModelSettingsService` 通过 `atomic_write` 更新 `models.yaml`，并用 `model_config_scope` 在任务开始时使用生效版本（[../server/README.md](../server/README.md)）。

## 使用方与边界

- **Agent 构建**：`build_rp_agent` 经节点工厂调用 `get_node_config`；`model_config_scope` 只在服务任务中使用。
- **直接导入**：`config.config` 的常量在多个模块导入期读取；修改 `.env` 后需要重启进程。
- **密钥边界**：API key 与连接串只在本模块与模型工厂中使用，服务端 `/settings` 只返回脱敏信息。
- **不提交本地文件**：`config/.env`、`config/models.yaml`、私钥文件属于本地配置。

## 阅读导航

- 上级：[系统总览](../README.md) · 模型工厂：[agent/utils/models/README.md](../agent/utils/models/README.md)
- 服务配置：[server/README.md](../server/README.md) · 根目录使用说明：[README.md](../README.md)
