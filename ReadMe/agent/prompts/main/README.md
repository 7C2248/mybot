# agent/prompts/main — 主 LLM 提示词

`main/` 承载主回复节点（图中注册名 `draft`）使用的提示词。当前只有一个子模块 `draft/`：`agent/node/draft.py` 在构建节点时获取提示词并缓存，运行时把世界状态、角色状态、用户状态及可选检查反馈拼接到提示词之后，再送入主模型。本目录 README 只说明模块分工与协作，具体输入变量、生成内容和消费方式见叶子文档。

## 子目录与节点

| 名称 | 类型 | 主要功能 | 协作对象 | 源码 | 文档 |
| --- | --- | --- | --- | --- | --- |
| `draft` | 提示词模块 | 生成主回复节点的系统提示词（核心准则、风格规范、角色档案块） | `create_draft_node`（`agent/node/draft.py`） | [draft.py](../../../../agent/prompts/main/draft.py) | [draft/README.md](draft/README.md) |

`main/__init__.py` 仅导出 `get_draft_prompt`；`agent/prompts/__init__.py` 中 `get_prompt("draft")` 分支虽然存在，但当前没有调用方，主链实际由 draft 节点直接调用 getter。

## 整体流程

```text
builder 注册 draft 节点（agent/builder.py:90）
  → create_draft_node(character_name, character_profile, tools) 构建时调用
      get_draft_prompt(character_name=..., language=..., character_profile=...)
  → prompt_base（字符串，含 <character_file> 角色档案块）
  → 运行时 system_content = prompt_base + 世界/角色/用户状态文本
      +（可选）记忆工具缺失说明 +（可选）<previous_draft> 与 <check_feedback>
  → SystemMessage + 消息历史送入 get_node_model("main")（已 bind_tools）
  → check 未通过时由 check_judge 路由回 draft，重复运行时拼接（最多 5 轮内容修订）
```

## 主要数据与依赖

- 构建参数：`character_name`、`language`（`builder.py` 未显式传 `language`，取默认 `"zh"`）、`character_profile`（builder 构建时预加载后传入）来自图构建函数。
- 档案来源：`character_profile` 为 `None` 时，由 `load_character_profile` 读取 `Character/<character_name>/profile_cn.md`（`en` 分支在读取前已返回空串，不会读档）；档案缺失会抛 `FileNotFoundError`。
- 下游：模型选择、工具绑定、重试与状态写回都由 `create_draft_node` 完成，提示词模块不感知工具与模型细节。

## 阅读导航

- 上级：[../README.md](../README.md)
- 子模块：[draft/README.md](draft/README.md)
- 相关类别：[../tools/README.md](../tools/README.md)
