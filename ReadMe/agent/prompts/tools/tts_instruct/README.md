# tts_instruct 提示词

## 职责与入口

- 所属类别：提示词模块（非图节点）。源码：[agent/prompts/tools/tts_instruct.py](../../../../../agent/prompts/tools/tts_instruct.py)。
- 注册与导出：`tools/__init__.py` 导入并列入 `__all__`（[agent/prompts/tools/__init__.py:9](../../../../../agent/prompts/tools/__init__.py)）；`agent/prompts/__init__.py` 再次导出，并提供 `get_prompt("tts_instruct")` 分发（[agent/prompts/__init__.py:46](../../../../../agent/prompts/__init__.py)）。
- 获取函数：`get_tts_instruct_prompt(character_file: str = "") -> str`（[tts_instruct.py:6](../../../../../agent/prompts/tools/tts_instruct.py)）；没有 `language` 参数，始终返回中文提示词。
- 消费节点：图节点 `tts`，提示词在内部函数 `_generate_segment_plans` 中获取（[agent/node/tts.py:119](../../../../../agent/node/tts.py)）。
- 触发时机：`commit_reply → participant_state_out → update_iter → tts`；节点仅当 `state["need_tts"]` 为真且最后一条消息是 AI 回复时执行（[agent/node/tts.py:195](../../../../../agent/node/tts.py)）。

## 调用链总览

```text
（构建）create_tts_node(character_name, character_file=...)
    → character_file 取参数或 _load_voice_profile(character_name)
      （Character/<name>/tts.md → profile_cn.md → profile_en.md → 任一 profile_*.md）

（运行）tts_node
    → need_tts / 最后消息类型校验
    → _parse_reply(content)                    提取对白与动作描述（「」消息丢弃）
    → _generate_segment_plans(segments, character_file)
        → get_prompt("tts_instruct", character_file=...)   （本提示词，SystemMessage）
        → HumanMessage(编号对白列表) → get_node_model("tts").ainvoke(...)
        → _parse_plans(content, len(segments))  对齐 instruct/pause
    → 合成与播放（asyncio.to_thread）
```

## 获取链

### B1. `get_tts_instruct_prompt`

- 定位与签名：`agent.prompts.tools.tts_instruct.get_tts_instruct_prompt(character_file: str = "") -> str`，同步函数，[源码 tts_instruct.py:6](../../../../../agent/prompts/tools/tts_instruct.py)。
- 调用方与条件：`_generate_segment_plans` 每次生成语气计划时通过 `get_prompt("tts_instruct", character_file=character_file)` 获取（[agent/node/tts.py:119](../../../../../agent/node/tts.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `character_file` | `str` | 默认 `""` | 角色语音/角色档案文本；由 `create_tts_node` 决定来源 |

- 隐式输入：无（不依赖语言归一，也没有其他文件读取）。
- 功能与内部调用：直接返回 f-string 模板；`character_file` 为空字符串时，模板内插入占位文本 `（无）`（[tts_instruct.py:15](../../../../../agent/prompts/tools/tts_instruct.py)），否则插入档案全文。
- 输出：完整 TTS 语气生成系统提示词字符串，末尾包含 `<character_file>…</character_file>` 块。
- 副作用与异常：无；不读文件、不调模型、不抛异常。

生成内容概要（不复制原文）：

| 部分 | 内容 |
| --- | --- |
| 角色定位 | 语音合成（TTS）的语气控制生成器，为角色回复中的每句对白生成语音描述与句间停顿 |
| 核心原则 | 冲突时以核心原则为准；允许符合角色设定的内容；角色档案以 `<character_file>` 块注入，缺失时显示 `（无）` |
| 语音描述维度 | ①基本身份（性别+年龄段）②音色质感 ③情感/语气为必填；④语速与节奏 ⑤风格/场景 ⑥细节修饰为可选 |
| 任务要求 | 对每句编号对白生成不超过 50 字的中文 instruct，须覆盖①~③且不得与档案冲突；为每句生成 0~3 秒的 pause；只输出 JSON 数组 `{"instruct","pause"}`，项数与对白行数一一对应，无解释与代码块 |

## 运行链

### R1. `_generate_segment_plans`（提示词消费）

- 定位与签名：`agent.node.tts._generate_segment_plans(segments: list[dict], character_file: str) -> list[dict]`，异步；[agent/node/tts.py:111](../../../../../agent/node/tts.py)。
- 调用方与条件：`create_tts_node.<locals>.tts_node` 在解析出非空对白后调用（[agent/node/tts.py:210](../../../../../agent/node/tts.py)）。

| 输入参数或字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `segments` | `list[dict]` | `_parse_reply` 结果 | 至少一项且 `text` 非空 | 每项含 `text`（对白）与 `description`（动作描述，可为空串） |
| `character_file` | `str` | `create_tts_node` 闭包 | 可为空串 | 注入提示词的 `<character_file>` 块 |

- 隐式输入：模型 `get_node_model("tts")`；提示词 B1 结果；解析辅助 `_parse_plans`。
- 功能与内部调用：
  1. 将每段对白格式化为 `"{序号}. 对白：{text} | 描述：{description or '无'}"` 并以换行连接；
  2. `llm.ainvoke([SystemMessage(get_prompt("tts_instruct", character_file=...)), HumanMessage(numbered)])`；
  3. 正文为空时，从 `additional_kwargs["reasoning_content"]` 的 `</thinking_process>` 之后截取（[agent/node/tts.py:122](../../../../../agent/node/tts.py)）；
  4. `_parse_plans(content, len(segments))`（[agent/node/tts.py:81](../../../../../agent/node/tts.py)）：清洗代码块后 `json.loads`，逐项取 `instruct`（str）与 `pause`（float，缺省 0.3，钳制在 0~3），项数不足补默认值、超出截断，与对白数量对齐。
- 输出与状态字段：

| 输出 | 类型 | 产生条件 | 含义与消费方 |
| --- | --- | --- | --- |
| `plans` | `list[dict]` | 正常返回或解析失败兜底 | 每项 `{"instruct": str, "pause": float}`；`tts_node` 拆成 `instructions`/`pauses` 后送合成 |
| 抛出的异常 | - | 模型调用异常 | 由 `tts_node` 捕获，改用全默认计划（空 instruct、0.3 秒停顿） |

- 副作用：一次模型请求。
- 异常与边界：JSON 解析失败返回全默认列表并记录日志；列表项非 dict、`pause` 非数值、项数不符都在 `_parse_plans` 内兜底；模型异常由调用方处理。
- 后续去向：`tts_node` 用 `plans` 生成 `instructions`/`pauses`，调用 `synthesize` 并按模型类型（`voice_design`/`custom_voice`/`base`）合成；合成/播放异常在有 `audio_sink` 时抛出，否则记录日志并跳过（[agent/node/tts.py:220](../../../../../agent/node/tts.py)）。

## 分支与异常链

- **`need_tts` 为假**：节点直接返回 `{}`，提示词不参与。
- **最后一条消息不是 AI 消息或没有对白**：跳过；有 `audio_sink`（如服务端流式场景）时抛 `ValueError("speech_empty")`，否则记录日志返回（[agent/node/tts.py:203](../../../../../agent/node/tts.py)）。
- **语气生成失败**：`_generate_segment_plans` 异常被捕获，`plans` 置空后使用默认值，语音仍会合成（[agent/node/tts.py:211](../../../../../agent/node/tts.py)）。
- **档案缺失**：`_load_voice_profile` 依次尝试 `tts.md`、`profile_cn.md`、`profile_en.md`、任一 `profile_*.md`；全部缺失时返回空串，提示词中显示 `（无）`（[agent/node/tts.py:62](../../../../../agent/node/tts.py)）。
- **无语言分支**：该提示词没有 EN 变体，任何语言配置下都输出中文。

## 输入输出示例

适用 R1（示意，非真实内容）：

```text
SystemMessage：tts_instruct 提示词（含 <character_file> 角色语音档案 </character_file>）
HumanMessage：
1. 对白：你回来了。 | 描述：我放下手里的书，抬头看向门口
2. 对白：外面冷，先把外套脱了。 | 描述：无
```

期望模型输出（经 `_parse_plans` 对齐）：

```json
[{"instruct": "年轻女性，声音清亮柔和，语气平静中带着关心", "pause": 0.4}, {"instruct": "年轻女性，音色清脆，语速稍快，温和提醒", "pause": 0.8}]
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md)
- 消费节点源码：[agent/node/tts.py](../../../../../agent/node/tts.py)
- 验证依据：静态阅读源码；本次未执行测试。
