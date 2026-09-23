# mybot 架构文档

本目录按 [README_SPEC.md](../README_SPEC.md) 编写，说明 `mybot` 当前实现：一个基于 LangGraph 的角色对话 Agent，使用 Postgres 保存对话 checkpoint 与角色长期记忆，对外提供本机 FastAPI 服务，支持桌面端、浏览器演示端和 CLI 共用同一套会话与记忆策略，并支持语音合成。

本页是文档总入口，只说明系统职责、入口、子系统与端到端流程；函数签名、参数和状态字段细节下沉到各级叶子文档。

## 系统目标与主要能力

- **角色对话**：主回复节点自行提取事实、判断感知并主动调用记忆检索工具，候选正文由独立检查节点校验后才提交为正式回复。
- **长期记忆**：按角色存储在 Postgres（父记忆 + 向量块子表），支持混合检索（向量 + 关键词 + 时间过滤 + Reranker 精排）；对话事件在后台独立线程整理为增删改计划并原子提交，不阻塞下一轮对话。
- **会话持久化**：UI 历史独立于 Agent 上下文裁剪，正式回复、运行状态和 SSE 事件入库；Agent 状态由 LangGraph checkpoint 保存。
- **多端接入**：本机 API 服务、CLI（HTTP 客户端）、React + Tauri 桌面端与浏览器演示端。
- **语音**：本机 Qwen TTS 合成角色对白，服务模式写出 WAV 资源供前端播放；CLI/图内仍可调用节点内播放。
- **模型配置**：`config/models.yaml` 按节点配置模型，服务端支持保存与显式应用（从下一任务生效）。

## 入口

| 入口 | 命令或文件 | 说明 |
| --- | --- | --- |
| 本机 API 服务 | `python -m server` | FastAPI，固定 `127.0.0.1`，默认端口 8765，见 [server/README.md](server/README.md) |
| CLI | `python start_cli.py` / `main.py` | 启动或复用本机 API 后进入交互客户端，见 [cli/README.md](cli/README.md) |
| 桌面 / 浏览器前端 | `frontend/`（`npm run dev` / `tauri`） | 桌面默认连接本机服务，浏览器默认演示模式，见 [frontend/README.md](frontend/README.md) |
| 独立记忆 Worker | `python -m agent.memory.worker` | 消费持久化记忆队列，见 [agent/memory/README.md](agent/memory/README.md) |
| 维护脚本 | `scripts/rebuild_character_memory.py` 等 | 一次性维护工具，见 [agent/memory/store/README.md](agent/memory/store/README.md) |

## 子系统

| 子系统 | 职责 | 文档 |
| --- | --- | --- |
| `agent/` | LangGraph 图构建、节点、工具、Prompt、状态协议、记忆子系统、共享工具 | [agent/README.md](agent/README.md) |
| `server/` | 本机 FastAPI：HTTP 接入、运行执行器、会话与历史、旧 CLI 导入、记忆运行时、语音、配置、角色资源、存储 | [server/README.md](server/README.md) |
| `cli/` | 通过 HTTP 接口使用桌面会话的 CLI 客户端与启动器 | [cli/README.md](cli/README.md) |
| `frontend/` | React + TypeScript + Vite + Tauri 桌面/浏览器界面 | [frontend/README.md](frontend/README.md) |
| `core/` | Postgres 连接池与 LangGraph checkpoint 生命周期 | [core/README.md](core/README.md) |
| `config/` | 运行配置、`.env` 加载与节点模型配置 | [config/README.md](config/README.md) |
| `utils/` | 跨应用共享的日志与时间工具 | [utils/README.md](utils/README.md) |

`Character/` 保存角色档案（`profile_cn.md`、`profile_en.md`、`tts.md`）与图片、音频、模型、技能等资源；由 `agent/utils/character.py` 与 `server/services/characters.py` 读取。

## 端到端流程

以一次桌面/CLI 对话为例：

```text
前端/CLI
  → POST /api/threads/{id}/runs            （提交原始文本与请求键，见 server/http/README.md）
  → mybot_ui.runs 持久队列
  → 对话执行器 RunRuntime 领取              （见 server/runtime/README.md）
  → AgentAdapter.execute                   （注入模型配置、角色档案、提交回调）
  → LangGraph Agent（agent/builder.py）
       begin_turn → apply_memory_results → limit_context → world_state_update
       → participant_state_in → draft ⇄ tools
       → check → commit_reply → participant_state_out → update_iter → tts
       → prepare_memory → enqueue_memory → END
  → commit_reply 回调先写正式历史，再允许图写 checkpoint
  → 前端通过 SSE 事件与消息历史读取结果
  → 独立记忆线程消费 memory_service.jobs，整理长期记忆（见 server/memory/README.md）
```

关键边界：

- **图调度与直接调用不同**：`draft` 到 `tools`、`check` 到 `commit_reply` 等由 LangGraph 条件边调度，不是前一函数直接调用后一函数；详见 [agent/builder/README.md](agent/builder/README.md)。
- **同步提交与后台整理分离**：`prepare_memory`/`enqueue_memory` 只投递不可变快照并结束本轮；记忆计算由独立 Worker 执行，结果在后续轮次由 `apply_memory_results` 应用；详见 [agent/node/memory/README.md](agent/node/memory/README.md) 与 [agent/memory/README.md](agent/memory/README.md)。
- **正式历史与模型上下文分离**：上下文裁剪（`limit_context`）不删除 UI 正式历史；详见 [agent/node/context/README.md](agent/node/context/README.md)。

## 代码组织约定

- 仅当前文件使用的辅助函数和内部类型保留在文件内，以 `_` 开头。
- 多个模块共用的辅助函数放到共同所属范围的 `utils/`；Agent 专用逻辑放在 `agent/utils/`，跨应用通用逻辑放在根目录 `utils/`。维护脚本直接复用 Agent 的分块接口，不再导入存储实现的私有函数。
- 共享状态和输入输出协议放在所属范围的 `classes/`。使用复数 `classes` 是因为 `class` 是 Python 关键字，不能写成常规的 `from agent.class.state import AgentState`。
- `agent/node/` 的 `__all__` 仅导出节点工厂、节点执行函数和路由；节点的共享状态处理从 `agent/utils/state.py` 导入，状态规范从 `agent/classes/state.py` 导入。
- Prompt 文件只定义相应节点的提示词；读取角色档案、语言选择等共享逻辑从 `agent/utils/` 导入。
- 工厂使用 `create_*_node`，节点执行函数使用动作名称，例如 `increment_iteration`、`update_world_state`。图通过 `prepare_memory → enqueue_memory` 交接后台任务，轮初通过 `apply_memory_results` 应用裁剪结果；成功入队后扣除快照覆盖的轮数。

## 验证与依据

各级文档区分“静态阅读源码所得”“已有测试覆盖”和“本次实际执行验证”。文档整理时未重新运行测试；服务与前端父文档保留 2026-09-21/22 的历史验收记录，Agent 各叶子列出对应的离线/集成测试文件。未验证项在各叶子“关联文档与验证依据”中如实标注。

## 阅读导航

- 系统总览（本页）
- [Agent 架构总览](agent/README.md)
- [服务端总览](server/README.md) · [CLI](cli/README.md) · [前端](frontend/README.md)
- [core](core/README.md) · [config](config/README.md) · [utils](utils/README.md)
- 参考资料：[LangGraph_CheckPoint_Postgres.md](../data/docs/LangGraph_CheckPoint_Postgres.md)（上游 checkpointer 说明与安全注意事项，存放于本地 `data/docs/`）
- 面向使用的项目入口与快速开始：[README.md](../README.md)
