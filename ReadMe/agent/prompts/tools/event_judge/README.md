# event_judge 提示词

## 职责与入口

- 所属类别：提示词模块（非图节点）。源码：[agent/prompts/tools/event_judge.py](../../../../../agent/prompts/tools/event_judge.py)。
- 注册与导出：`tools/__init__.py` 导入并列入 `__all__`（[agent/prompts/tools/__init__.py:5](../../../../../agent/prompts/tools/__init__.py)）；`agent/prompts/__init__.py` 再次导出，并提供 `get_prompt("event_judge")` 分发（[agent/prompts/__init__.py:34](../../../../../agent/prompts/__init__.py)）。
- 获取函数：`get_event_judge_prompt(language: str = "zh") -> str`（[event_judge.py:9](../../../../../agent/prompts/tools/event_judge.py)）。
- **当前状态：当前未接入主链。** 全仓库没有任何调用方，事件判断已改由 `agent/node/event.py` 的 `event_judge` 函数按轮数与 token 阈值完成，其文档字符串明确说明“不再调用语义事件判断模型”（[agent/node/event.py:36](../../../../../agent/node/event.py)）。

## 调用链总览

当前没有运行链路。仅存在以下注册关系：

```text
tools/__init__.py 导出 get_event_judge_prompt
  → agent/prompts/__init__.py 导出 + get_prompt("event_judge") 分支
  → （无消费方）
```

替代实现（当前实际生效，位于另一模块）：

```text
tts → event_judge(state) -> bool          （agent/node/event.py:33，同步路由函数）
  条件：memory_storage_enabled 为真、无 pending/active 记忆任务、need_event_judge 为真
  区间内：MINIMUM_ITERATIONS <= iteration <= MAXIMUM_ITERATIONS 时比较上下文 token 与
          MEMORY_TOKEN_THRESHOLD；iteration > MAXIMUM_ITERATIONS 时强制进入
  返回：True → prepare_memory；False → END
```

## 获取链

### B1. `get_event_judge_prompt`（无消费方）

- 定位与签名：`agent.prompts.tools.event_judge.get_event_judge_prompt(language: str = "zh") -> str`，同步函数，[源码 event_judge.py:9](../../../../../agent/prompts/tools/event_judge.py)。
- 调用方与条件：无。`get_prompt("event_judge")` 分支同样无调用方。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `language` | `str` | 默认 `"zh"` | 语言标识；`normalize_language` 后为 `"en"` 时返回英文分支 |

- 隐式输入：`normalize_language`（[agent/utils/language.py:4](../../../../../agent/utils/language.py)）。
- 功能与内部调用：归一语言；`"en"` 时调用 `_get_event_judge_prompt_en()`（[event_judge.py:15](../../../../../agent/prompts/tools/event_judge.py)，返回空字符串），否则调用 `_get_event_judge_prompt_zh()`（[event_judge.py:19](../../../../../agent/prompts/tools/event_judge.py)）。
- 输出：事件完成判断提示词字符串；中文版本非空，EN 版本为空字符串。
- 副作用与异常：无；不读文件、不调模型、不抛异常。

生成内容概要（不复制原文）：

| 部分 | 内容 |
| --- | --- |
| 角色与后果 | 事件完成检测器；判断 YES 会立即触发对话总结、写入记忆、清理原始消息队列，因此只有“有意义、相对自洽、值得长期保存的事件完成点”才输出 YES |
| 输入范围 | 只收到自上次记忆更新以来累积的对话，看不到完整历史；只需基于眼前内容判断 |
| 判断标准 A（宏观） | 场景转换、冲突/目标解决、明确收束信号 |
| 判断标准 B（微观） | 有内容的日常活动自然结束、情绪弧达到不可逆的稳定结果、清晰持续的行为模式切换 |
| 判断标准 C（软边界） | 互动足够深后的节奏/注意力转移、前一线索完全结束后的空间/语境重新聚焦 |
| 关键规则 | 记忆价值优先；暂定停顿默认 NO 以保护会被清理的上下文；嵌套事件需形成独立可记忆片段；撤回/修正按是否真实重启核心冲突判断；模糊时默认 NO |
| 输出契约 | 只输出 `YES` 或 `NO`，无其他文字、标点或解释；附 6 组示例（冒险节点、未完成任务、日常结束、情绪稳定收束、琐碎闲聊、情绪撤回） |

## 运行链

当前无运行链。提示词内容设计为单轮输出 `YES`/`NO`，若未来恢复语义判断，需要由调用方自行提供“自上次记忆更新以来累积的对话”并解析返回值；本次文档不描述不存在的调用与解析逻辑。

## 分支与异常链

- 不适用：无消费方，因此没有模型调用、重试、超时或降级路径可记录。
- EN 语言：`_get_event_judge_prompt_en()` 返回空字符串；即使被调用也不会产生有效判断依据。
- 当前替代逻辑的分支见上文“替代实现”与 [agent/node/event.py](../../../../../agent/node/event.py)，属于节点路由，不由本提示词控制。

## 输入输出示例

无实际调用示例。按提示词自身约定，模型输出应形如：

```text
YES
```

或

```text
NO
```

该示例仅说明提示词的输出契约，不代表当前运行行为。

## 关联文档与验证依据

- 上级：[../README.md](../README.md)
- 相关提示词：[../event_summary/README.md](../event_summary/README.md)
- 当前实际判断实现：[agent/node/event.py](../../../../../agent/node/event.py)
- 验证依据：静态阅读源码与全仓库调用方检索；本次未运行测试。“当前未接入主链”为检索结论。
