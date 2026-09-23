# agent/prompts/tools — 工具与辅助节点提示词

`tools/` 承载除主回复节点之外各环节的提示词：候选回复检查、角色/用户状态更新、TTS 语气生成，以及后台记忆处理使用的查询生成、事件总结、记忆分块。这些提示词由对应节点或后台处理器获取后填充运行数据，再交给模型；本目录只说明模块分工与消费关系，不虚构运行调用链。

## 子目录与节点

| 名称 | 类型 | 主要功能 | 协作对象 | 源码 | 文档 |
| --- | --- | --- | --- | --- | --- |
| `check` | 提示词模块 | 候选回复的格式、风格、人称检查指令 | `create_check_node`（`agent/node/check.py`） | [check.py](../../../../agent/prompts/tools/check.py) | [check/README.md](check/README.md) |
| `chunking` | 提示词模块 | 记忆语义分块、关键词与事件日期提取 | `agent/utils/chunking.py`（`get_chunk_prompt`）；`get_keyword_extract_prompt` **当前未接入主链** | [chunking.py](../../../../agent/prompts/tools/chunking.py) | [chunking/README.md](chunking/README.md) |
| `event_judge` | 提示词模块 | 事件完成点判断（YES/NO） | 无消费方，**当前未接入主链** | [event_judge.py](../../../../agent/prompts/tools/event_judge.py) | [event_judge/README.md](event_judge/README.md) |
| `event_summary` | 提示词模块 | 后台记忆总结：add/update/delete/clean 决策与记忆写法规范 | `process_memory_snapshot`（`agent/memory/processor.py`，后台 Worker） | [event_summary.py](../../../../agent/prompts/tools/event_summary.py) | [event_summary/README.md](event_summary/README.md) |
| `memory_query` | 提示词模块 | 记忆总结阶段的日志体检索查询生成 | `process_memory_snapshot`（`agent/memory/processor.py`，后台 Worker） | [memory_query.py](../../../../agent/prompts/tools/memory_query.py) | [memory_query/README.md](memory_query/README.md) |
| `participant_state` | 提示词模块 | 角色与用户状态的联合更新规则 | `create_participant_state_node`（`agent/node/participant_state.py`，`in`/`out` 两实例） | [participant_state.py](../../../../agent/prompts/tools/participant_state.py) | [participant_state/README.md](participant_state/README.md) |
| `tts_instruct` | 提示词模块 | 按对白生成 TTS 语气描述与句间停顿 | `_generate_segment_plans`（`agent/node/tts.py`） | [tts_instruct.py](../../../../agent/prompts/tools/tts_instruct.py) | [tts_instruct/README.md](tts_instruct/README.md) |

## 整体流程

提示词模块之间不互相调用，按“注册/导出 → 获取 → 参数填充 → 消费节点”协作：

```text
tools/__init__.py 导出 8 个 getter
  → agent/prompts/__init__.py 再导出，并提供 get_prompt 类型键分发

主链（图节点，同步于本轮调度）：
  get_prompt("check")              → agent/node/check.py
  get_prompt("participant_state")  → agent/node/participant_state.py（participant_state_in / _out）
  get_prompt("tts_instruct")       → agent/node/tts.py

后台链（enqueue_memory 入队后由 Worker 执行）：
  get_prompt("memory_query")       → agent/memory/processor.py::process_memory_snapshot
  get_prompt("event_summary")      → agent/memory/processor.py::process_memory_snapshot
  get_chunk_prompt()               → agent/utils/chunking.py::_chunk_with_llm
                                     （经 store.prepare_memory 被 CRUD 与后台计划复用）

未接入：
  get_event_judge_prompt、get_keyword_extract_prompt
```

## 主要数据与依赖

- **语言处理**：带 `language` 参数的 getter 使用 `normalize_language`；`_CHECK_EN`、`_get_chunk_prompt_en`、`_get_keyword_extract_prompt_en`、`_get_event_judge_prompt_en`、`_get_event_summary_prompt_en`、`_get_memory_query_prompt_en` 均为空字符串，`participant_state` 的 EN 变体只有一个换行；`tts_instruct` 没有语言参数，始终输出中文提示词。当前调用方均未显式传 `language`，实际生效的都是中文。
- **运行数据**：检查正文与最近用户输入、双方状态与证据消息对、编号对白、世界状态与消息历史、记忆文本等由消费方拼接，提示词模块不接收结构化参数（`draft` 的 `character_name` 除外）。
- **模型依赖**：消费方分别使用 `get_node_model` 的 `check`、`participant_state`、`tts`、`chunking`、`memory_query`、`memory_summary` 节点模型；`event_judge` 无模型依赖（未接入）。
- **队列边界**：`memory_query` / `event_summary` / `chunking` 的调用发生在 `agent/memory/worker.py` 处理任务期间，与图节点执行不在同一上下文；数据库写入由 `MemoryJobRepository.finish` 统一提交。

## 阅读导航

- 上级：[../README.md](../README.md)
- 相关类别：[../main/README.md](../main/README.md)
- 记忆服务：[../../memory/README.md](../../memory/README.md)
