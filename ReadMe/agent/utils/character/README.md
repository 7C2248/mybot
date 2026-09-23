# character — 角色档案目录定位与读取

## 职责与入口

- 所属类别：`agent/utils/` 支撑模块，**不是图节点**，无图注册名、无路由。
- 源码：[`agent/utils/character.py`](../../../../agent/utils/character.py)（模块 docstring：读取图构建和回复提示词共用的角色档案）。
- 职责：把角色名映射到项目根目录下的 `Character/<角色名>/`，并按语言读取其中的角色档案；目录内同时承载 `images/`、`audio/`、`model/`、`skills/` 等资源文件夹。
- 公开入口两个：`character_dir`（目录定位）与 `load_character_profile`（档案读取）。
- 调用方式：同步函数，由构建阶段和提示词获取阶段直接调用，不经过框架调度。

## 调用链总览

```text
构建阶段
  builder.build_rp_agent
    └─ R2 load_character_profile(character_name)   [仅 character_profile is None 且非 recovery_only]
         ├─ R2.1 character_dir(character_name)          → Character/<name>
         ├─ R2.2 language.normalize_language(language)  → "zh" / "en"
         └─ R2.3 读取 profile_cn.md / profile_en.md
              └─ 目标缺失 → R2.4 sorted(glob("profile_*.md")) 回退取第一个
    → 返回值作为 character_profile 传入 create_draft_node

运行时（draft 构建）
  prompts.main.draft.get_draft_prompt(character_name, language, character_profile=None)
    └─ R2 load_character_profile(character_name, language)   [character_profile 为 None 时]

运行时（tts 构建）
  agent.node.tts._load_voice_profile(character_name)
    └─ R1 character_dir(character_name)
         └─ 依次尝试 tts.md → profile_cn.md → profile_en.md → sorted(glob("profile_*.md"))
```

`_load_voice_profile` 是 `agent/node/tts.py` 的内部函数，只复用 `character_dir`，不走 `load_character_profile`，因此回退顺序与 R2 不同（优先 `tts.md`）。

## 运行链

### R1. `character_dir`

- 定位与签名：`character_dir(character_name: str) -> Path`，[`agent/utils/character.py:10`](../../../../agent/utils/character.py)，同步函数。
- 调用方与条件：`load_character_profile`（R2.1）无条件调用；`agent/node/tts.py` 的 `_load_voice_profile` 在构建 TTS 节点时调用（[`agent/node/tts.py:66`](../../../../agent/node/tts.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `character_name` | `str` | 必填 | 角色名（如 `SuLi`），来自 `build_rp_agent` 的入参或调用方；直接参与路径拼接，不做合法性校验 |

隐式输入：`__file__` 的解析结果——`Path(agent/utils/character.py).resolve().parents[2]` 即项目根目录。

功能与内部调用：

1. 以模块文件为锚点向上回溯两级得到项目根目录（`parents[0]`=`agent/utils`、`parents[1]`=`agent`、`parents[2]`=项目根）。
2. 拼出 `<项目根>/Character/<character_name>` 并返回，不检查目录是否存在。

输出：`pathlib.Path` 对象，指向 `Character/<角色名>`；调用方决定随后读取哪个文件。

副作用：无（不创建目录、不读取文件）。

异常与边界：无显式异常；`character_name` 含路径分隔符或为空时只影响拼接结果，越界访问不在本函数拦截。

后续去向：返回 R2.1 继续拼档案路径；返回 `_load_voice_profile` 继续读取语音档案。

### R2. `load_character_profile`

- 定位与签名：`load_character_profile(character_name: str, language: str = "zh") -> str`，[`agent/utils/character.py:15`](../../../../agent/utils/character.py)，同步函数。
- 调用方与条件：
  - `agent/builder.py` 的 `build_rp_agent`：仅当 `character_profile is None and not recovery_only` 时调用，结果作为构建参数传给 `create_draft_node`（[`agent/builder.py:57`](../../../../agent/builder.py)）。
  - `agent/prompts/main/draft.py` 的 `get_draft_prompt`：仅当传入的 `character_profile is None` 时调用（[`agent/prompts/main/draft.py:17`](../../../../agent/prompts/main/draft.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `character_name` | `str` | 必填 | 角色名，传给 R1 |
| `language` | `str` | 默认 `"zh"` | 语言标识，先经 `normalize_language` 归一；当前两个调用方均未显式传入，实际始终走中文分支 |

隐式输入：

- 模块常量 `_LANGUAGE_FILES = {"zh": "profile_cn.md", "en": "profile_en.md"}`（[`agent/utils/character.py:7`](../../../../agent/utils/character.py)）。
- `Character/<角色名>/` 目录下的实际文件布局（含 `profile_cn.md`、`profile_en.md`、`tts.md` 及资源文件夹）。

功能与内部调用：

1. 调用 R1 `character_dir(character_name)` 得到目录。
2. 调用 `agent.utils.language.normalize_language(language)`（见 [`../language/README.md`](../language/README.md)）得到 `"zh"` 或 `"en"`，再从 `_LANGUAGE_FILES` 取目标文件名。
3. 若目标文件存在，以 UTF-8 读取并返回其完整内容。
4. 目标不存在时，`sorted(directory.glob("profile_*.md"))` 取排序后的第一个候选（按文件名升序，通常 `profile_cn.md` 先于 `profile_en.md`）并返回其内容。
5. 无任何候选时抛 `FileNotFoundError`。

输出：角色档案的完整文本（`str`），由 draft 节点提示词嵌入 `<character_file>` 段。

副作用：只读文件系统，无写入。

异常与边界：

- 档案目录不存在或无 `profile_*.md`：抛 `FileNotFoundError(f"未找到角色档案: {directory / 'profile_*.md'}")`，由调用方向上抛出（`build_rp_agent`、`get_draft_prompt` 均不捕获）。
- 读取时使用 `encoding="utf-8"`；编码错误不做降级处理，向上抛出。

后续去向：返回值在构建阶段成为 `create_draft_node(character_profile=...)` 的缓存输入；在提示词阶段被拼进系统提示词。

## 分支与异常链

| 条件 | 行为 | 终点 |
| --- | --- | --- |
| `language` 归一为 `en` 且 `profile_en.md` 存在 | 返回英文档案 | R2 返回 |
| 目标语言档案缺失、目录内存在其他 `profile_*.md` | 按文件名排序取第一个（通常为中文档案） | R2 返回 |
| 目录不存在或无任何 `profile_*.md` | 抛 `FileNotFoundError` | 调用方 `build_rp_agent` / `get_draft_prompt` 向上抛出，构建失败 |
| `character_profile` 已由调用方提供 | 不进入 R2 | 使用调用方文本（`builder` 的 CLI/服务路径会传入 `character_profile`） |

`agent/node/tts.py` 的 `_load_voice_profile` 是独立分支：先读 `tts.md`，再依次尝试 `profile_cn.md`、`profile_en.md`，最后 `sorted(glob("profile_*.md"))`，全空时返回 `""` 而不是抛异常（[`agent/node/tts.py:62`](../../../../agent/node/tts.py)）。

## 输入输出示例

适用步骤 R2，角色目录 `Character/SuLi/`：

```text
输入: character_name="SuLi", language="zh"
内部: character_dir → <项目根>/Character/SuLi
      _LANGUAGE_FILES["zh"] → "profile_cn.md"
输出: profile_cn.md 的 UTF-8 全文（str）
```

回退示例（假设目录内只有 `profile_en.md`）：

```text
输入: character_name="Demo", language="zh"
目标: Character/Demo/profile_cn.md 不存在
回退: sorted(["profile_en.md"])[0] → profile_en.md 全文
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)（agent/utils 总览）
- 相关模块：[`../language/README.md`](../language/README.md)（`normalize_language`）、[`../../prompts/README.md`](../../prompts/README.md)（draft 提示词对档案的消费）
- 调用方源码：[`agent/builder.py:57`](../../../../agent/builder.py)、[`agent/prompts/main/draft.py:17`](../../../../agent/prompts/main/draft.py)、[`agent/node/tts.py:62`](../../../../agent/node/tts.py)
- 目录布局依据：`Character/SuLi/` 实际包含 `profile_cn.md`、`profile_en.md`、`tts.md`、`images/`、`audio/`、`model/`、`skills/`
- 验证情况：本页为静态阅读源码所得；当前没有针对 `agent/utils/character.py` 的专门单元测试，`tests/test_module_layout.py` 的离线导入扫描会实际导入本模块（[`tests/test_module_layout.py:129`](../../../../tests/test_module_layout.py)）。本次文档编写未实际执行测试。
