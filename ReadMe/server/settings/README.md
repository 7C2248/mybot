# 模型设置与能力报告：ModelSettingsService 与脱敏配置

## 职责与入口

本页覆盖 `server/services/model_settings.py`、`server/services/settings.py`、`server/services/health.py` 与 `server/routes/settings.py`、`server/routes/health.py`：模型配置的“保存版本/生效版本”分离、显式应用、并发门锁、脱敏报告与就绪状态。

| 文件 | 类型 | 职责 |
| --- | --- | --- |
| [server/services/model_settings.py](../../../server/services/model_settings.py) | 服务 | `ModelSettingsService`：`_read/snapshot/get/save/apply`，两个门锁 |
| [server/services/settings.py](../../../server/services/settings.py) | 服务 | `read_settings`：脱敏配置与能力报告 |
| [server/services/health.py](../../../server/services/health.py) | 服务 | `readiness`：数据库就绪与 schema 版本 |
| [server/classes/settings.py](../../../server/classes/settings.py) | 协议 | `PUBLIC_NODES`、`ProfileWrite`、`NodeModelSettings`、`ModelSettingsWrite`、`ApplyModelSettings` |
| [server/routes/settings.py](../../../server/routes/settings.py) | 路由 | `/api/settings*` |
| [server/routes/health.py](../../../server/routes/health.py) | 路由 | `/api/health`、`/api/ready` |

上游：`app.py` lifespan 创建 `ModelSettingsService(settings, gate)` 并注入 `RunRuntime`；`gate` 与对话/语音执行器共享，`memory_gate` 为内部锁。下游：`config/models.yaml` 文件、执行器的 `models.snapshot()`。

## 调用链总览

```text
构建阶段
B1 ModelSettingsService.__init__ ── 建立 gate/memory_gate/lock，启动时读取 active
B2 read_settings（无状态函数，路由每次调用）

运行阶段
R1 ModelSettingsService._read ── 读文件、校验结构、计算保存版本哈希
R2 ModelSettingsService.snapshot ── 执行器取生效配置与版本（深拷贝）
R3 GET /api/settings/models → ModelSettingsService.get ── 公开节点与版本状态
R4 PUT /api/settings/models → ModelSettingsService.save ── 版本冲突与 llama_cpp 规则后原子写
R5 POST /api/settings/models/apply → ModelSettingsService.apply ── 两个门锁 + 版本校验后切换 active
R6 GET /api/settings → readiness + read_settings ── 脱敏报告
R7 GET /api/ready → readiness ── 数据库与能力
```

## 构建链

### B1. `ModelSettingsService.__init__`

- 定位与签名：[server/services/model_settings.py](../../../server/services/model_settings.py) 的 `def __init__(self, settings, gate)`。
- 输入：`settings`（`ServiceSettings`，读取 `model_config` 路径）、`gate`（`app.py` lifespan 创建的 `threading.Lock`，对话/语音共用）。
- 功能：`self.memory_gate = threading.Lock()`；`self.lock = threading.RLock()`；`self.active=None`、`self.active_version=None`；尝试 `self.active, self.active_version = self._read()`，`ServiceError` 被忽略（启动时配置缺失不阻断服务，后续调用按需报 503）。

### B2. `read_settings`

- 定位与签名：[server/services/settings.py](../../../server/services/settings.py) 的 `def read_settings(settings: ServiceSettings, *, database_ready: bool) -> SettingsStatus`（同步，路由经 `asyncio.to_thread` 调用）。
- 输入：`settings`（`environment` 用于凭据存在性判断）、`database_ready`。
- 输出：`SettingsStatus`。字段语义见 R6。

## 运行链

### R1. `ModelSettingsService._read`

- 定位与签名：`def _read(self)`。
- 功能：`raw = self.settings.model_config.read_bytes()`；`yaml.safe_load`；要求顶层为 dict、`nodes` 为 dict、`defaults`（若有）为 dict；返回 `(data, hashlib.sha256(raw).hexdigest())`。**保存版本 = 文件字节的 SHA-256**。
- 异常：文件缺失/格式无效 → 503 `model_config_unavailable`（“模型配置文件缺失或格式无效”）。不改写文件。

### R2. `snapshot`

- 定位与签名：`def snapshot(self)`。
- 调用方：`AgentAdapter.execute`（每次运行开始）、`SpeechService.execute`、`MemoryRuntime._serve`、`routes/runs.require_runtime`。
- 功能：`with self.lock:`；`active is None` → 503 `model_config_unavailable`；返回 `(deepcopy(self.active), self.active_version)`。
- 输出：生效配置深拷贝与生效版本。执行器据此进入 `model_config_scope(data)`，运行中配置不变；节点模型实例按任务快照缓存（[config/README.md](../../config/README.md)）。

### R3. `get`

- 定位与签名：`def get(self)`。
- 调用方：`GET /api/settings/models`；`save`/`apply` 成功后也调用它返回最新视图。

功能：`with self.lock:`；`_read()` 得到磁盘 `(data, version)`；对 `sorted(PUBLIC_NODES & data["nodes"].keys())` 逐节点：`merged = data.defaults | data["nodes"][name]`；只取 `NodeModelSettings` 已声明字段（`provider/model/thinking/reasoning_effort`）中实际存在的键，`NodeModelSettings.model_validate(...)` 成功后以 `model_dump()` 输出；校验失败（例如旧配置含自定义枚举）**跳过该节点且不回显**。

输出字段：

| 字段 | 含义 |
| --- | --- |
| `saved_version` | 磁盘文件哈希（保存版本） |
| `active_version` | 当前生效版本；启动时读入，`apply` 后更新 |
| `pending_changes` | `saved_version != active_version` |
| `nodes` | 公开节点（`PUBLIC_NODES` 与磁盘节点的交集）的合并配置 |
| `effective_from` | 固定 `"next_run"` |
| `runtime_busy` | `gate.locked() or memory_gate.locked()` |

`PUBLIC_NODES`（[server/classes/settings.py](../../../server/classes/settings.py)）：`main`、`participant_state`、`memory_query`、`memory_summary`、`chunking`、`tts`。`read_settings` 的 `_PUBLIC_NODES` 只有前五项（不含 `tts`），两处范围不同，属于现状。

### R4. `save`

- 定位与签名：`def save(self, request)`；`request` 为 `ModelSettingsWrite`。
- 调用方：`PUT /api/settings/models`。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `request.expected_version` | `str`（64 字符） | 必填 | 客户端读取到的 `saved_version` |
| `request.nodes` | `dict[str, NodeModelSettings]` | 必填，键 ⊆ `PUBLIC_NODES`（Pydantic 校验） | 要写入的节点字段 |

功能：`with self.lock:` → `_read()`；`version != expected_version` → 409 `version_conflict`；逐节点：
- `previous = data["nodes"].get(name) or {}`；非 dict → 409 `model_config_unavailable`；
- `node.provider == "llama_cpp"` 且 `not previous.get("model_path")` → 409 `local_model_not_configured`（“请先在本机配置该节点的本地模型路径”）；本地路径只能由本机 YAML 预先配置，不能通过接口提交；
- `data["nodes"][name] = previous | node.model_dump()`：**合并保留原有私有字段**（如 `model_path`、`base_url` 等），只覆盖公开字段。
- `atomic_write(self.settings.model_config, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))`（原子写见 [http 叶子](../http/README.md) R5）；返回 `self.get()`。

输出：同 R3 的设置视图。副作用：磁盘保存版本变化；`active` 不变，直到 `apply`。保存不调用远端“测试连接”。

### R5. `apply`

- 定位与签名：`def apply(self, expected_version)`；`expected_version` 为 64 字符字符串。
- 调用方：`POST /api/settings/models/apply`。

功能：
1. `self.gate.acquire(blocking=False)` 失败 → 409 `runtime_busy`（“当前有对话、语音或记忆任务，结束后再应用设置”）。
2. `self.memory_gate.acquire(blocking=False)` 失败 → 409 `runtime_busy`（“当前有后台记忆任务…”）；两把锁都拿到后：
   - `with self.lock:` → `_read()`；`version != expected_version` → 409 `version_conflict`；
   - `self.active = deepcopy(data)`；`self.active_version = version`。
3. `finally` 依次释放 `memory_gate`、`gate`；返回 `self.get()`。

输出：设置视图（`pending_changes` 变回 `False`）。语义：生效从下一任务开始（`effective_from="next_run"`），运行中配置不变；服务重启读取磁盘保存版本作为 `active`。

### R6. `GET /api/settings` 与 `read_settings` 脱敏规则

路由：`state = await readiness(database, configured=bool(settings.db_url))` → `asyncio.to_thread(read_settings, settings, database_ready=state.database=="connected")` → `result.read_only = False` → 计算 `capabilities.chat/speech`（同 `/ready`）。

`read_settings` 行为：
1. `yaml.safe_load(settings.model_config.read_text())`；顶层或 `nodes` 非 dict、`defaults` 非 dict → 视为 `invalid`（`models={}`）。
2. `FileNotFoundError` → `model_configuration="missing"`。
3. 其余 `OSError/ValueError/TypeError/YAMLError` → `model_configuration="invalid"`。
4. 对 `_PUBLIC_NODES = ("main","participant_state","memory_query","memory_summary","chunking")` 中存在于磁盘的节点：`config = defaults | node`；`provider` 只允许 `deepseek/moonshot/llama_cpp`，否则标 `unknown`；`deepseek` 检查 `environment["DEEPSEEK_API_KEY"]`，`moonshot` 检查 `environment["KIMI_API_KEY"]`，只输出布尔 `credentials_configured`；`llama_cpp` 检查 `environment["LOCAL_GGUF_MODEL_PATH"]` 是否为存在的文件，只输出布尔 `local_model_available`；`configured = bool(config.get("model")) or bool(local_available)`。

输出 `SettingsStatus`：`mode="service"`、`read_only`（由路由置 `False`）、`model_configuration`（`present/missing/invalid`）、`models`（每节点 `provider/configured/credentials_configured/local_model_available`）、`database_configured`、`capabilities`（`memories=database_ready`，`chat/speech` 由路由补充）。

**永不返回**：API key、连接串、磁盘路径、`base_url`、`defaults`、检查节点等非公开节点、节点私有字段。凭据只以布尔形式出现；`ModelSettingsService.get` 同样只回显 `NodeModelSettings` 的四个字段。

### R7. `readiness`

- 定位与签名：[server/services/health.py](../../../server/services/health.py) 的 `async def readiness(database, *, configured)`。
- 功能：`await database.ping()`（任何异常按未连接）；返回 `ReadyStatus(status, database, schema_version, capabilities)`：
  - `database`：`connected` / `unavailable`（已配置但 ping 失败）/ `not_configured`；
  - `schema_version`：连接时取 `database.schema_version`（最后一个迁移版本），否则 `None`；
  - `capabilities.memories=connected`，其余由调用方补充。
- 路由 `GET /api/ready`：`capabilities.chat = database=="connected" and runtime.ready and models.active is not None`；`capabilities.speech = capabilities.chat`；非 ready 时响应 503。
- 边界：`health` 只检查 HTTP 进程（返回 `HealthStatus(version)`）；`ready` 报告数据库和运行能力。能力为 `true` 不代表已验证模型凭据、模型连通性或 TTS 权重；实际推理失败通过运行/语音任务状态报告。

## 分支与异常链

| 条件 | 处理 | 终点 |
| --- | --- | --- |
| 模型配置文件缺失/格式无效 | 503 `model_config_unavailable`（`snapshot/_read`）；`GET /settings` 报告 `missing/invalid` | R1/R2/R6 |
| 保存版本与 `expected_version` 不符 | 409 `version_conflict` | R4/R5 |
| 请求节点不在 `PUBLIC_NODES` | Pydantic 校验失败 → 422 `invalid_request` | 路由 |
| 节点含未知/受保护字段 | 保存时合并保留在磁盘但不回显；不通过接口编辑 | R3/R4 |
| 选择 `llama_cpp` 但本机未配置路径 | 409 `local_model_not_configured` | R4 |
| 对话/语音任务执行中应用 | 409 `runtime_busy`（`gate` 被占用） | R5 |
| 后台记忆任务执行中应用 | 409 `runtime_busy`（`memory_gate` 被占用） | R5 |
| 未配置数据库 | `GET /settings` 仍可用，`capabilities.memories=false`；`GET /ready` 503 `not_configured` | R6/R7 |
| 已配置数据库但连接失败 | `GET /ready` 503 `unavailable`；`chat/speech=false` | R7 |
| 应用成功后 | 从下一任务生效；运行中配置不变；重启读取磁盘保存版本 | R5 |

## 输入输出示例

`GET /api/settings/models`（R3，虚构）：

```json
{"saved_version": "9f2a...64hex", "active_version": "9f2a...64hex",
 "pending_changes": false, "effective_from": "next_run", "runtime_busy": false,
 "nodes": {"main": {"provider": "deepseek", "model": "deepseek-chat",
                    "thinking": "enabled", "reasoning_effort": "max"}}}
```

`PUT /api/settings/models`（R4）：

```json
{"expected_version": "9f2a...64hex",
 "nodes": {"main": {"provider": "deepseek", "model": "deepseek-chat",
                    "thinking": "disabled", "reasoning_effort": "medium"}}}
```

返回视图的 `saved_version` 变为新哈希、`pending_changes=true`；随后 `POST /api/settings/models/apply`（`expected_version` 为新哈希）把 `active_version` 更新为新哈希。

`GET /api/settings`（R6，节选）：

```json
{"mode": "service", "read_only": false, "model_configuration": "present",
 "models": {"main": {"provider": "deepseek", "configured": true,
                     "credentials_configured": true, "local_model_available": null}},
 "database_configured": true,
 "capabilities": {"memories": true, "chat": true, "speech": true, "profile_write": true}}
```

## 关联文档与验证依据

- 上级：[服务总览](../README.md)；系统总览：[README/README.md](../../README.md)
- 同级：[runtime](../runtime/README.md)（`snapshot` 与 `memory_gate` 使用）· [memory](../memory/README.md)（记忆线程门锁）· [http](../http/README.md)（路由、`readiness` 与错误信封）· [characters](../characters/README.md)（`atomic_write` 的另一使用方）
- 配置：[config/README.md](../../config/README.md)（`models.yaml` 结构、`model_config_scope`、节点模型缓存）
- 测试依据：[tests/server/test_api.py](../../../tests/server/test_api.py)（设置路由与脱敏）、[tests/server/test_execution.py](../../../tests/server/test_execution.py)（配置应用与运行互斥）、[tests/test_model_scopes.py](../../../tests/test_model_scopes.py)
- 迁移自原 `server/README.md`（原文件已移除）的规则：允许节点为 `main/participant_state/memory_query/memory_summary/chunking/tts`；保存时合并 YAML 并保留原有私有字段；PUT 只保存，随后 POST apply；对话、TTS 或记忆任务执行时应用返回 409 `runtime_busy`；成功后从下一任务使用新快照；服务重启读取磁盘保存版本；每个对话记录 `model_version` 和 `profile_version`；检查节点、defaults、API key、连接串、本地路径和 `base_url` 不通过接口编辑或返回；选择 llama_cpp 前需在本机配置该节点路径；保存和应用不调用远端“测试连接”。
- 验证记录（迁移自原文档，未在本次编写中重跑）：2026-09-22 验收覆盖并发模型配置与缓存隔离；本轮仅静态核对源码。
