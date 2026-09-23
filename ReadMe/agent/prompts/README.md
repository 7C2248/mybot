# agent/prompts — Prompt 注册与使用

`agent/prompts/` 汇总 Agent 运行期间使用的静态提示词：`main/` 面向主 LLM 的回复生成，`tools/` 面向检查、状态、TTS 等辅助节点与后台记忆处理器。Prompt 本身不是图节点，也不发起模型调用；它由节点或后台处理器在构建/运行时获取、填充，再随其他上下文一起交给模型。本目录只说明各提示词模块的注册、导出与消费关系，不虚构运行调用链。

## 子目录与节点

| 名称 | 类型 | 主要功能 | 协作对象 | 源码 | 文档 |
| --- | --- | --- | --- | --- | --- |
| `main/` | 提示词类别目录 | 主 LLM 节点的系统提示词 | `agent/node/draft.py` | [main/__init__.py](../../../agent/prompts/main/__init__.py) | [main/README.md](main/README.md) |
| `tools/` | 提示词类别目录 | 工具/辅助节点与后台记忆处理器的提示词 | 除 draft 外的各节点、`agent/memory/processor.py`、`agent/utils/chunking.py` | [tools/__init__.py](../../../agent/prompts/tools/__init__.py) | [tools/README.md](tools/README.md) |

### 注册与消费一览

`agent/prompts/__init__.py` 是统一入口：既逐个导出各 getter，也提供 `get_prompt(prompt_type, **kwargs)` 分发（[agent/prompts/__init__.py:32](../../../agent/prompts/__init__.py)）。`get_prompt` 从 kwargs 取 `language`，缺省 `"zh"`；类型键只覆盖 `event_judge`、`event_summary`、`memory_query`、`participant_state`、`check`、`draft`、`tts_instruct`，未知类型抛 `ValueError`。

| 获取函数 | `get_prompt` 类型键 | 当前消费方 | 状态 |
| --- | --- | --- | --- |
| `get_draft_prompt` | `draft` | `agent/node/draft.py`（直接调用 getter，未走 `get_prompt`） | 主链 |
| `get_check_prompt` | `check` | `agent/node/check.py` | 主链 |
| `get_chunk_prompt` | 无对应键 | `agent/utils/chunking.py`（直接 import） | 主链 |
| `get_event_summary_prompt` | `event_summary` | `agent/memory/processor.py`（后台 Worker 上下文） | 主链 |
| `get_memory_query_prompt` | `memory_query` | `agent/memory/processor.py`（后台 Worker 上下文） | 主链 |
| `get_participant_state_prompt` | `participant_state` | `agent/node/participant_state.py` | 主链 |
| `get_tts_instruct_prompt` | `tts_instruct` | `agent/node/tts.py` | 主链 |
| `get_event_judge_prompt` | `event_judge` | 无 | **当前未接入主链** |
| `get_keyword_extract_prompt` | 无对应键 | 无 | **当前未接入主链** |

说明：

- `get_prompt` 的 `draft` 与 `event_judge` 分支均已注册，但当前没有调用方。draft 节点直接 import `get_draft_prompt`；事件判断已改为按轮数与 token 阈值决定，不再调用语义模型（[agent/node/event.py:33](../../../agent/node/event.py)）。
- `get_prompt` 没有 `chunking` / `keyword_extract` 类型键，`get_chunk_prompt` 由 `agent/utils/chunking.py` 直接引入。
- `get_keyword_extract_prompt` 虽被导出，但分块失败时的关键词回退走的是正则函数 `extract_keywords`，不调用该提示词（[agent/utils/chunking.py:54](../../../agent/utils/chunking.py)）。

## 整体流程

提示词模块之间不互相调用，协作关系是“定义 → 注册/导出 → 获取并填充 → 交给模型”：

```text
各 prompt 模块（返回字符串常量）
  → main/__init__.py、tools/__init__.py 导出 getter
  → agent/prompts/__init__.py（get_prompt 分发 + 具名 getter）
  → 消费方节点/处理器在构建或运行时获取
      ├─ agent/node/draft.py             ← get_draft_prompt（构建时缓存）
      ├─ agent/node/check.py             ← get_prompt("check")
      ├─ agent/node/participant_state.py ← get_prompt("participant_state")（in/out 两个实例）
      ├─ agent/node/tts.py               ← get_prompt("tts_instruct")
      ├─ agent/memory/processor.py       ← get_prompt("memory_query")、get_prompt("event_summary")
      └─ agent/utils/chunking.py         ← get_chunk_prompt
  → 与状态文本、角色档案、消息历史等拼接后作为 SystemMessage 或用户内容送入模型
```

其中 `agent/memory/processor.py` 的两条链路运行在独立后台 Worker 进程/任务中（图节点 `enqueue_memory` 入队后返回，不由本轮图同步执行）；`event_judge` 与 `keyword_extract` 停在“注册/导出”一步，见 [tools/README.md](tools/README.md)。

## 主要数据与依赖

- **语言选择**：所有带 `language` 参数的 getter 先调用 `normalize_language`（[agent/utils/language.py:4](../../../agent/utils/language.py)）：字符串以小写 `en` 开头返回 `"en"`，否则（含空值）返回 `"zh"`。多数 `*_EN` 变体是空字符串或仅换行，当前调用方也都未显式传 `language`，因此实际生效的始终是中文提示词；各叶子文档逐一记录真实差异。
- **角色档案**：`get_draft_prompt` 与 `get_tts_instruct_prompt` 会把角色档案文本嵌入提示词；档案由 `load_character_profile`（[agent/utils/character.py:15](../../../agent/utils/character.py)）或 TTS 语音档案加载函数读取，属于提示词模块唯一的文件读取副作用。
- **运行数据**：世界/角色/用户状态文本（`agent.utils.state` 的 `prepare_*`）、消息历史（`format_history`）、证据 JSON 等由消费方在获取提示词之后另行拼接，不属于提示词模块的职责。
- **模型依赖**：消费方通过 `agent/utils/models.py` 的 `get_node_model` 取模型（主链使用 `main`、`check`、`participant_state`、`tts`、`chunking`、`memory_query`、`memory_summary` 等节点模型）；提示词模块本身不接触模型、数据库与队列。

## 阅读导航

- 上级：[agent/README.md](../README.md)
- 子目录：[main/README.md](main/README.md)、[tools/README.md](tools/README.md)
- 统一入口源码：[agent/prompts/__init__.py](../../../agent/prompts/__init__.py)
- 补充专题：[../memory/README.md](../memory/README.md)（记忆服务整体设计，含后台 Worker 边界）
