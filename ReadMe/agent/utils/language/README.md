# language — 语言标识归一化

## 职责与入口

- 所属类别：`agent/utils/` 支撑模块，**不是图节点**，无图注册名、无路由。
- 源码：[`agent/utils/language.py`](../../../../agent/utils/language.py)（模块 docstring：提示词和角色档案共用的语言选择）。
- 职责：把调用方传入的任意语言标识（如 `"EN-us"`、`None`、`"zh"`）归一为项目支持的两种取值 `"zh"` 或 `"en"`，供角色档案选择与提示词分支使用。
- 公开入口：`normalize_language`，单函数模块，无内部辅助函数。
- 调用方式：同步函数，在角色档案读取和各提示词 getter 内直接调用。

## 调用链总览

```text
R1 normalize_language(language="zh") -> "zh" | "en"

调用方
  agent/utils/character.py load_character_profile   → 选择 profile_cn.md / profile_en.md
  agent/prompts/main/draft.py get_draft_prompt      → 选择中/英提示词分支与档案语言
  agent/prompts/tools/check.py get_check_prompt     → 选择中/英检查提示词
  agent/prompts/tools/chunking.py get_chunk_prompt / get_keyword_extract_prompt
  agent/prompts/tools/event_summary.py get_event_summary_prompt
  agent/prompts/tools/memory_query.py get_memory_query_prompt
  agent/prompts/tools/participant_state.py get_participant_state_prompt
  agent/prompts/tools/event_judge.py get_event_judge_prompt   ← 该提示词当前无调用方
```

## 构建链

无工厂、无缓存、无依赖；单表达式纯函数。

## 运行链

### R1. `normalize_language`

- 定位与签名：`normalize_language(language: str = "zh") -> str`，[`agent/utils/language.py:4`](../../../../agent/utils/language.py)，同步函数。
- 调用方与条件：见上表；各调用方在需要选择语言分支时无条件调用，未传参时使用默认值 `"zh"`。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `language` | `str` | 默认 `"zh"` | 语言标识；来自调用方参数或 `get_prompt` 的 `kwargs.get("language", "zh")`（[`agent/prompts/__init__.py:33`](../../../../agent/prompts/__init__.py)） |

隐式输入：无。

功能与内部调用：单条表达式 `return "en" if (language or "zh").lower().startswith("en") else "zh"`。

1. 假值（`None`、`""`）先被替换为 `"zh"`，避免空值导致 `.lower()` 失败；
2. 转小写后判断是否以 `"en"` 开头，是则返回 `"en"`；
3. 其余情况（含 `"zh"`、`"zh-CN"`、`"ja"` 等）一律返回 `"zh"`。

输出：`"zh"` 或 `"en"`。

副作用：无。

异常与边界：`language` 为非字符串真值（如数字）时 `.lower()` 抛 `AttributeError`，调用方均传字符串或 `None`。判断基于前缀，因此 `"EN-us"`、`"english"` 都归一为 `"en"`。

后续去向：返回值由调用方用于选择文件名或提示词分支；本函数不读取文件、不返回提示词。

## 分支与异常链

| 条件 | 返回值 | 消费方行为 |
| --- | --- | --- |
| `language` 为 `None` 或 `""` | `"zh"` | 读中文档案、取中文提示词 |
| 小写后以 `"en"` 开头 | `"en"` | 读 `profile_en.md`；多数英文提示词当前为空字符串（见各 prompt 叶子） |
| 其他任意值 | `"zh"` | 回落到中文 |

## 输入输出示例

与 `tests/test_module_layout.py:79-80` 的断言一致：

```text
normalize_language("EN-us")  → "en"
normalize_language(None)     → "zh"
normalize_language()         → "zh"
normalize_language("zh-CN")  → "zh"
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)（agent/utils 总览）
- 调用方文档：[`../character/README.md`](../character/README.md)、[`../../prompts/README.md`](../../prompts/README.md)
- 已有测试覆盖（本次未执行）：`tests/test_module_layout.py:79-80`
- 验证情况：本页为静态阅读源码所得；调用方清单按当前源码的 import 关系逐一核对，未在本次文档编写中实际执行测试。
