# state — 世界/角色/用户状态规范化与提示词格式化

## 职责与入口

- 所属类别：`agent/utils/` 支撑模块，**不是图节点**，无图注册名、无路由。
- 源码：[`agent/utils/state.py`](../../../../agent/utils/state.py)（模块 docstring：各节点共用的状态规范化及提示词格式化）。
- 职责：把 `AgentState` 中的状态字典规范化为固定字段集，并生成可直接拼进系统提示词的 `<world_state>` / `<character_state>` / `<user_state>` 文本块。世界状态的时间**始终由系统时钟重新生成**，只有天气从已存状态读取。
- 公开入口：`prepare_world_state`、`prepare_character_state`、`prepare_user_state`；内部函数：`system_world_time`、`_time_period`、`_prepare_participant_state`。
- 调用方式：同步函数，由图节点与后台记忆处理器直接调用。

## 调用链总览

```text
R1 system_world_time()  ← 只被 R2 调用
     └─ R6 _time_period(hour)

R2 prepare_world_state(world_state)
     └─ R1（时间总是重新生成）；仅 world_state["weather"] 被读取

R3 prepare_character_state(character_state) ─┐
     └─ R5 _prepare_participant_state(..., _CHARACTER_STATE_KEYS, "character_state")
R4 prepare_user_state(user_state)            ─┘
     └─ R5 _prepare_participant_state(..., _USER_STATE_KEYS, "user_state")

调用方
  agent/node/world_state.py update_world_state        → R2（只用 state_data，丢弃文本）
  agent/node/participant_state.py（in/out 两个实例）  → R2/R3/R4（state_data + 文本）
  agent/node/draft.py                                 → R2/R3/R4（只用文本）
  agent/memory/processor.py process_memory_snapshot   → R2（只用文本，后台上下文）
```

## 构建链

无工厂、无缓存、无依赖注入；`_WEEKDAYS` 等为模块级常量。

## 运行链

### R1. `system_world_time`

- 定位与签名：`system_world_time() -> dict`，[`agent/utils/state.py:32`](../../../../agent/utils/state.py)，同步函数。
- 调用方与条件：仅 R2 内部无条件调用。

隐式输入：系统时钟 `datetime.now()`；模块常量 `_WEEKDAYS`。

功能与内部调用：

1. `now = datetime.now()`（本机本地时间，无时区处理）；
2. `date = now.strftime("%Y-%m-%d")`；
3. `weekday = _WEEKDAYS[now.weekday()]`（`星期一`～`星期日`）；
4. `period = _time_period(now.hour)`（R6）。

输出：`{"date": str, "weekday": str, "period": str}`。

副作用：无。异常与边界：无（系统时钟正常时）。后续去向：返回 R2。

### R6. `_time_period`

- 定位与签名：`_time_period(hour: int) -> str`，[`agent/utils/state.py:13`](../../../../agent/utils/state.py)，同步内部函数。
- 调用方与条件：仅 R1 调用。

功能与内部调用：按顺序判断严格不等式，第一个命中者返回：

| 条件 | 返回值 | 实际命中的小时 |
| --- | --- | --- |
| `2 < hour < 6` | 凌晨 | 3、4、5 |
| `5 < hour < 8` | 早上 | 6、7 |
| `8 < hour < 11` | 上午 | 9、10 |
| `11 < hour < 13` | 中午 | 12 |
| `13 < hour < 17` | 下午 | 14、15、16 |
| `17 < hour < 19` | 傍晚 | 18 |
| `19 < hour < 24` | 晚上 | 20、21、22、23 |
| 其余 | 深夜 | 0、1、2，以及落在区间端点上的 8、11、13、17、19 |

输出：中文字符串。副作用：无。异常与边界：`hour` 非整数时可比较但结果按 Python 规则；小时为 5 时先命中“凌晨”，小时为 6 时命中“早上”。端点小时（8/11/13/17/19）因严格不等式落到“深夜”，这是当前实现的实际边界行为。

### R2. `prepare_world_state`

- 定位与签名：`prepare_world_state(world_state: dict | None) -> tuple[dict, str]`，[`agent/utils/state.py:42`](../../../../agent/utils/state.py)，同步函数。
- 调用方与条件：
  - `agent/node/world_state.py` 的 `update_world_state`：读取 `state.get("world_state")`，只用返回的 `state_data`，天气更新逻辑在外层（[`agent/node/world_state.py:14`](../../../../agent/node/world_state.py)）；
  - `agent/node/participant_state.py` 的节点：把返回的文本拼进模型输入（[`agent/node/participant_state.py:64`](../../../../agent/node/participant_state.py)）；
  - `agent/node/draft.py` 的节点：只用文本（[`agent/node/draft.py:60`](../../../../agent/node/draft.py)）；
  - `agent/memory/processor.py` 的 `process_memory_snapshot`：把文本作为后台检索/总结的背景（[`agent/memory/processor.py:174`](../../../../agent/memory/processor.py)）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `world_state` | `dict | None` | 必填（可为 `None`） | `AgentState["world_state"]`；只读取其中的 `weather` 键 |

隐式输入：系统时钟（经 R1）。

功能与内部调用：

1. 初始化 `state_data = {"time": system_world_time(), "weather": None}`——时间总是重新生成，**不读取** `world_state["time"]`；
2. 若 `world_state` 是 dict，`state_data["weather"] = world_state.get("weather")`（缺失为 `None`）；
3. 生成固定四行文本块：

```text
<world_state>
date: {date},
weekday: {weekday},
time_period: {period},
weather: {weather},
</world_state>
```

输出：`(state_data, state_text)`。`state_data` 的键固定为 `time`（含 `date`/`weekday`/`period`）与 `weather`；`weather` 原样透传（不校验类型）。

副作用：无。异常与边界：`world_state` 为非 dict（含 `None`）时时间照常生成、天气为 `None`；旧状态中的 `time`、其他未知键被丢弃。

后续去向：`update_world_state` 用 `state_data` 覆盖 `AgentState["world_state"]`，并在外层用 `fetch_current_weather` 的成功结果替换 `weather`；其他调用方把 `state_text` 拼进提示词。

### R3. `prepare_character_state`

- 定位与签名：`prepare_character_state(character_state: dict | None) -> tuple[dict, str]`，[`agent/utils/state.py:63`](../../../../agent/utils/state.py)，同步函数。
- 调用方与条件：`agent/node/participant_state.py`（角色/用户状态联合更新，in/out 共用同一节点函数）、`agent/node/draft.py`（生成系统提示词时）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `character_state` | `dict | None` | 必填（可为 `None`） | `AgentState["character_state"]`；只读取固定键 |

功能与内部调用：调 R5，键集为 `_CHARACTER_STATE_KEYS = ("location", "mood", "body", "clothing", "hearing")`（[`agent/utils/state.py:6`](../../../../agent/utils/state.py)），标签为 `character_state`。

输出：`(state_data, state_text)`，`state_data` 恰好包含上述 5 键，缺失键值为 `None`。

副作用：无。异常与边界：非 dict 输入按空 dict 处理，全部键为 `None`。

后续去向：`participant_state` 节点把 `state_data` 与模型返回的增量合并后写回 `AgentState["character_state"]`；`draft` 节点只用文本。

### R4. `prepare_user_state`

- 定位与签名：`prepare_user_state(user_state: dict | None) -> tuple[dict, str]`，[`agent/utils/state.py:68`](../../../../agent/utils/state.py)，同步函数。
- 调用方与条件：与 R3 相同（participant_state 节点、draft 节点）。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `user_state` | `dict | None` | 必填（可为 `None`） | `AgentState["user_state"]`；只读取固定键 |

功能与内部调用：调 R5，键集为 `_USER_STATE_KEYS = ("location", "mood", "body", "clothing")`（无 `hearing`，因为感知判断只以角色的听觉范围为依据，见源码注释），标签为 `user_state`。

输出：`(state_data, state_text)`，`state_data` 恰好包含上述 4 键；`hearing` 即使存在于输入也会被忽略。

副作用：无。异常与边界：非 dict 输入按空 dict 处理。

后续去向：同 R3。

### R5. `_prepare_participant_state`

- 定位与签名：`_prepare_participant_state(state: dict | None, keys: tuple[str, ...], tag: str) -> tuple[dict, str]`，[`agent/utils/state.py:73`](../../../../agent/utils/state.py)，同步内部函数。
- 调用方与条件：仅 R3/R4 调用，二者只差键集与标签。

功能与内部调用：

1. `source = state if isinstance(state, dict) else {}`；
2. `state_data = {key: source.get(key) for key in keys}`——只保留指定键，缺失或显式为 `null` 均为 `None`；
3. 生成文本块：`<{tag}>\n` + 每个键一行 `key: value,\n` + `</{tag}>\n`，值为 `None` 时输出字面量 `None`。

输出：`(state_data, state_text)`。副作用：无。异常与边界：不校验值的类型与内容；非 dict 输入不报错。

后续去向：返回 R3/R4。

## 分支与异常链

| 条件 | 行为 | 终点 |
| --- | --- | --- |
| `world_state` 含旧 `time` | 被丢弃，时间取系统时钟 | R2 输出当前时间 |
| `world_state` 为 `None`/非 dict | 时间照常，`weather=None` | R2 输出 |
| 状态字典缺键 | 对应值为 `None`，文本行仍输出 | R3/R4/R5 |
| 用户状态含 `hearing` | 被过滤 | R4 输出不含该键 |
| 小时落在 8/11/13/17/19 | `_time_period` 返回“深夜” | R1 输出 |

## 输入输出示例

适用 R2（与 `tests/test_module_layout.py:61-73` 的断言一致，日期取实际系统日期）：

```text
输入: prepare_world_state({"time": {"date": "1999-01-01"}, "weather": "晴", "ignored": 1})
输出 state_data: {"time": {"date": "<今天>", "weekday": "星期三", "period": "下午"}, "weather": "晴"}
输出 state_text:
<world_state>
date: <今天>,
weekday: 星期三,
time_period: 下午,
weather: 晴,
</world_state>
```

适用 R3/R4（与 `tests/test_module_layout.py:74-78` 的断言一致）：

```text
prepare_character_state({"location": "客厅", "mood": None})
  → ({"location": "客厅", "mood": None, "body": None, "clothing": None, "hearing": None}, ...)

prepare_user_state({"location": "客厅", "hearing": "far"})
  → ({"location": "客厅", "mood": None, "body": None, "clothing": None}, ...)
    # 返回值不含 "hearing"
```

适用 R2 在 `update_world_state` 中的状态增量（天气请求成功时）：

```text
输入 state: {"world_state": {"weather": "晴"}}
外部: fetch_current_weather() → "少云 32°C"
返回: {"world_state": {"time": {"date": "<今天>", ...}, "weather": "少云 32°C"}}
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)（agent/utils 总览）
- 相关模块：[`../weather/README.md`](../weather/README.md)（`update_world_state` 中的天气来源）、[`../../classes/state/README.md`](../../classes/state/README.md)（`AgentState` 字段定义）、[`../../node/world_state/README.md`](../../node/world_state/README.md)、[`../../node/participant_state/README.md`](../../node/participant_state/README.md)、[`../../node/draft/README.md`](../../node/draft/README.md)
- 调用方源码：[`agent/node/world_state.py:14`](../../../../agent/node/world_state.py)、[`agent/node/participant_state.py:62`](../../../../agent/node/participant_state.py)、[`agent/node/draft.py:60`](../../../../agent/node/draft.py)、[`agent/memory/processor.py:174`](../../../../agent/memory/processor.py)
- 已有测试覆盖（本次未执行）：`tests/test_module_layout.py:59-80`（时间始终取当天、天气透传、键集差异）、`tests/test_module_layout.py:181-185`（`update_world_state` 使用打桩天气更新）
- 验证情况：本页为静态阅读源码所得；`_time_period` 的端点行为按条件表达式推导，测试只断言返回值属于 8 个时间段之一，未逐一覆盖端点。本次文档编写未实际执行测试。
