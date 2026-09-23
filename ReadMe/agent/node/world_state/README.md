# world_state 节点（世界状态更新）

## 职责与入口

- 所属类别：图节点（无工厂，异步函数）。
- 源码：[agent/node/world_state.py](../../../../agent/node/world_state.py)
- 图注册名：`world_state_update` → `update_world_state`（[agent/builder.py](../../../../agent/builder.py)）。
- 上游/下游：`limit_context` 固定边到 `world_state_update`，再由固定边到 `participant_state_in`。
- 触发时机：每轮一次，在上下文裁剪后、状态更新前执行。

## 调用链总览

| 步骤 | 函数 | 关系 |
| --- | --- | --- |
| R1 | `update_world_state(state)` | 图调度 |
| R1.1 | `prepare_world_state(world_state)` | 直接函数调用（[../../utils/state/README.md](../../utils/state/README.md)） |
| R1.2 | `fetch_current_weather()` | 异步直接调用（[../../utils/weather/README.md](../../utils/weather/README.md)） |

## 运行链

### R1. `update_world_state`

- 定位与签名：`async def update_world_state(state: AgentState)`，[agent/node/world_state.py:13](../../../../agent/node/world_state.py#L13)。
- 调用方与条件：LangGraph 固定边调度，每轮无条件执行。

| 输入字段 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `world_state` | `dict` | checkpoint | 可为 `None` | 只读取其中的 `weather`；时间字段被系统时间覆盖 |

隐式输入：系统时钟、和风天气配置（`QWEATHER_*`）、进程内天气缓存；日志器 `node.world_state`。

功能与内部调用：

1. 调用 `prepare_world_state(state.get("world_state"))`（R1.1）：返回规范化字典 `state_data` 与提示词文本；`time` 始终由 `system_world_time()` 重新生成（date/weekday/period），`weather` 沿用输入值。
2. `await fetch_current_weather()`（R1.2）请求实时天气；返回 `"少云 32°C"` 形式文本或 `None`。
3. 仅当结果非 `None` 且与旧值不同时，写入 `state_data["weather"]` 并记录一条 info 日志；失败或相同则保留旧值。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| `world_state` | `dict` | 总是 | 完整替换：`{"time": {date, weekday, period}, "weather": str|None}` | `draft`、`participant_state_*`、记忆快照、服务端 `state.updated` 事件 |

副作用：可能发起一次 HTTP 请求（缓存未命中时）；仅在天气变化时写日志。返回值中的字典是新构造的对象，不是原地修改旧状态。

异常与边界：

- 天气未配置或请求失败时 `fetch_current_weather` 返回 `None`，本节点不抛异常、保留旧天气（可能为 `None`）。
- 天气缓存 TTL 由 `QWEATHER_CACHE_TTL` 控制（默认 1200 秒），缓存命中时不会发起请求。
- 时间总是按进程当前时间更新；checkpoint 修复流程不会调用本节点，避免改写历史时间（见 [server/services/agent.py](../../../../server/services/agent.py) 的 `public_state`）。

后续去向：返回增量由框架合并，固定边到 `participant_state_in`。

## 分支与异常链

- **天气获取失败/未配置**：不更新 `weather`，其余照常返回。
- **天气与旧值相同**：不写日志，结果相同。
- **首次运行**：`world_state` 为空，`weather` 为 `None`，时间照常生成。

## 输入输出示例

适用 R1，天气从无到有：

```text
输入：{"world_state": {"time": {"date": "2026-09-01", ...}, "weather": null}}
（系统时间 2026-09-23，天气接口返回 "晴 25°C"）
输出：{"world_state": {"time": {"date": "2026-09-23", "weekday": "星期三", "period": "下午"},
                      "weather": "晴 25°C"}}
```

## 关联文档与验证依据

- 上级：[../README.md](../README.md) · 下游：[../participant_state/README.md](../participant_state/README.md)
- 状态格式化：[../../utils/state/README.md](../../utils/state/README.md) · 天气客户端：[../../utils/weather/README.md](../../utils/weather/README.md)
- 依据：`agent/node/world_state.py`；`tests/test_weather.py` 覆盖天气客户端离线行为；本次未执行测试。
