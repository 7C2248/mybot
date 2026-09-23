# mybot

`mybot` 是一个基于 LangGraph 的角色对话 Agent 项目，使用 Postgres 保存对话 checkpoint 和角色记忆，支持 CLI 和桌面对话、记忆混合检索与语音合成。桌面程序自动启动或复用本机 API，已接入真实会话与历史、运行状态与断线恢复、角色档案写回、模型设置保存/生效和 TTS 音频播放。

架构与代码说明统一在 [`README/`](README/README.md)：先读[系统总览](README/README.md)，再按目录进入 [Agent](README/agent/README.md)、[服务端](README/server/README.md)、[CLI](README/cli/README.md)、[前端](README/frontend/README.md)、[core](README/core/README.md)、[config](README/config/README.md)、[utils](README/utils/README.md)。

## 快速开始

1. 复制 `config/.env_example` 为 `config/.env`。
2. 填写 `DB_URL`、模型 API key 和本地模型路径。
3. 配置和风天气实时天气（可选）：填写 `QWEATHER_*` 凭据与 `QWEATHER_LATITUDE/LONGITUDE`，并把 Ed25519 私钥文件放到 `config/` 下。未配置时世界状态只写入系统时间，天气保持为空。
4. 安装依赖：

```powershell
pip install -r requirements.txt
```

### 本机 API 服务

```powershell
python -m pip install -r server/requirements.txt
python -m server
```

默认地址 `http://127.0.0.1:8765`，接口文档位于 `/docs`。服务沿用 `config/.env` 的数据库连接，自动在 `mybot_ui` schema 应用新增表迁移；后台执行对话和语音任务，另由独立记忆线程消费持久队列，记忆整理期间可继续对话。设置 `MYBOT_API_RUNS=0` 可只浏览数据与编辑配置。桌面默认连接真实服务，浏览器默认演示，可在“设置 → 连接与语音”切换，两种模式数据独立。配置项、接口和恢复边界见[服务端总览](README/server/README.md)。

### CLI

完成本地配置并启动 PostgreSQL 后，Windows 双击 `start_cli.cmd`，或运行：

```powershell
python start_cli.py --character-name SuLi --thread-id your-session-id
# 新建会话，只检索、不存储
python start_cli.py --character-name SuLi --new-thread --memory-retrieval on --memory-storage off
# 已有 API 时可直接调用客户端；只修改策略并退出
python main.py --thread-id <桌面会话UUID> --memory-storage on --configure-only
```

`start_cli.cmd` 优先使用 `MYBOT_PYTHON`、项目 `.venv/venv`、PATH 中的 Python。启动器复用已有本机 API，不存在时隐藏启动服务；退出只停止自己启动的服务。桌面和 CLI 共用会话、正式历史和记忆策略；新建或导入的会话默认两项关闭，升级前已有桌面会话保持原有策略。交互命令：

```text
/memory                 查看当前会话策略
/memory retrieval on    开启检索（off 关闭）
/memory storage on      开启存储（off 关闭）
/memory on              同时开启两项（off 同时关闭）
/retry                  明确重试最近失败的运行
exit                    退出
```

聊天历史始终保存，模型上下文裁剪独立于记忆存储。详细参数、恢复语义与断线处理见 [CLI 客户端](README/cli/client/README.md) 和 [CLI 启动器](README/cli/launcher/README.md)。

### 桌面 / 浏览器

见[前端总览](README/frontend/README.md)。本机可直接双击 `frontend/src-tauri/target/release/mybot-desktop.exe`；开发运行 `cd frontend; npm ci; npm run dev`。

### 后台记忆 Worker

后台记忆默认由 API 内部线程消费，也可单独运行 `python -m agent.memory.worker`，使用相同的权限核对、持久队列和提交检查。Worker 停止时未完成任务仍在表中，重新启动后自动继续；重试耗尽的任务保留为 `failed`，可单独重试：

```powershell
python -m agent.memory.worker --retry 123 --once
```

重建角色记忆向量：

```powershell
python scripts/rebuild_character_memory.py --character_name SuLi
```

## 离线验证

```powershell
python -B -m unittest discover -s tests -v
```

覆盖回复生成与检查、记忆工具循环、失败重试、状态更新、历史裁剪、任务交接与续跑、模块导入、共享辅助逻辑与数据库生命周期。默认测试隔离模型服务和数据库，不请求模型 API，也不加载模型权重。

显式执行 PostgreSQL/pgvector 集成测试（需要本地 `DB_URL` 用户拥有创建数据库权限；测试自动创建并删除独立临时数据库，不修改现有角色数据）：

```powershell
$env:MYBOT_MEMORY_DB_TESTS = '1'
python -B -m unittest tests.test_memory_service_postgres -v
Remove-Item Env:MYBOT_MEMORY_DB_TESTS
```

服务与前端测试命令见[服务端总览](README/server/README.md)与[前端总览](README/frontend/README.md)。

## 项目结构

```text
mybot/
├── agent/                  # Agent 图与角色对话逻辑
│   ├── builder.py          # LangGraph 状态图构建
│   ├── classes/            # AgentState、检查/状态/记忆协议、重排器类型
│   ├── memory/             # 角色记忆、持久化任务队列、计算与独立 Worker
│   ├── node/               # 节点工厂、节点执行与路由
│   ├── prompts/            # Prompt 模板注册表
│   ├── tools/              # LangChain 工具及默认工具集注册表
│   └── utils/              # Agent 共用的模型、分块、状态、文本、档案工具
├── config/                 # 可提交的运行配置与本地 .env 示例
├── core/                   # 数据库连接池与 checkpoint 生命周期
├── server/                 # 本机 FastAPI、运行队列、历史/SSE、档案、模型与音频服务
├── cli/                    # 与桌面共用 HTTP 接口的 CLI 客户端
├── frontend/               # React + Tauri 桌面/浏览器界面
├── scripts/                # 一次性维护脚本和实验脚本
├── tests/                  # 离线回归；classes/ 存放共享测试夹具
├── utils/                  # 跨应用通用工具：日志、时间
├── README/                 # 分层架构文档（本项目的说明入口）
├── main.py                 # CLI 入口
├── start_cli.py            # 自动启动或复用 API，然后进入 CLI
└── start_cli.cmd           # Windows 双击启动入口
```

代码组织约定与模块归属见[系统总览](README/README.md#代码组织约定)。
