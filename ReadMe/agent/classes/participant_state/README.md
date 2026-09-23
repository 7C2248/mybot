# UserStateUpdate / CharacterStateUpdate / ParticipantStateUpdate（participant_state 节点结构化输出协议）

## 职责与入口

- 所属类别：节点结构化输出协议（`agent/classes/participant_state.py`），不是图节点，没有工厂、路由或可执行入口。
- 源码：[`participant_state.py`](../../../../agent/classes/participant_state.py)
- 生产与消费集中在 [`agent/node/participant_state.py`](../../../../agent/node/participant_state.py) 的 `create_participant_state_node(trigger)`：`participant_state_in` 使用 `trigger="user"`，`participant_state_out` 使用 `trigger="reply"`，两个实例共享同一套 schema。
- 产物最终合并进 `AgentState` 的 `character_state` 与 `user_state`。

## 定义

### `UserStateUpdate`

```python
class UserStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    location: str | None = None
    mood: str | None = None
    body: str | None = None
    clothing: str | None = None
```

| 字段 | 类型 | 默认值 | 语义 |
| --- | --- | --- | --- |
| `location` | `str \| None` | `None` | 用户所在位置 |
| `mood` | `str \| None` | `None` | 用户情绪 |
| `body` | `str \| None` | `None` | 用户身体状态 |
| `clothing` | `str \| None` | `None` | 用户穿着 |

### `CharacterStateUpdate`

继承 `UserStateUpdate` 的四个字段，并追加：

| 字段 | 类型 | 默认值 | 语义 |
| --- | --- | --- | --- |
| `hearing` | `Literal["same_room","near","far","unknown"] \| None` | `None` | 角色听觉范围：同房间 / 附近 / 远 / 未知；用户状态没有该字段，感知判断只以角色听觉为依据 |

### `ParticipantStateUpdate`

```python
class ParticipantStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    character_state: CharacterStateUpdate
    user_state: UserStateUpdate
```

| 字段 | 类型 | 约束 | 语义 |
| --- | --- | --- | --- |
| `character_state` | `CharacterStateUpdate` | 必填，无默认值 | 角色状态增量 |
| `user_state` | `UserStateUpdate` | 必填，无默认值 | 用户状态增量 |

三个模型均为 `extra="forbid"`（拒绝未声明字段）且 `strict=True`（拒绝隐式类型转换，如用字符串冒充列表、用数字冒充字符串）；测试覆盖了非法字段类型与多余字段一律整体拒绝。

## 构造或校验

1. 模型返回文本先经 `content_text`、`strip_timestamps`、`strip_code_fence` 清洗，再用 `ParticipantStateUpdate.model_validate_json(content)` 严格解析。
2. 外层两个字段必填：`{"character_state": {...}}` 这类缺少 `user_state` 的“半包”会抛 `ValidationError`，整次尝试作废；只有两份状态都校验成功才一起写回，避免只更新一方。
3. `model_dump(exclude_unset=True)` 区分“未提供”与“显式 `null`”：
   - 未提供的字段不进入增量，旧值保留；
   - 显式 `null` 保留在增量中，用于清除已失效且无法确定的新状态。
4. 重试策略：`_STATE_MAX_RETRIES = 2`（最多 3 次尝试），单次调用超时 `_STATE_TIMEOUT = 90` 秒，失败间休眠 1 秒；全部失败返回 `{}`，不改动任何状态。

## 生产方

| 生产位置 | 产物 | 触发条件与输入 |
| --- | --- | --- |
| `create_participant_state_node.<locals>.node`（[`agent/node/participant_state.py`](../../../../agent/node/participant_state.py)） | `ParticipantStateUpdate` | `_message_pair(messages, trigger)` 返回非空消息对时；system prompt 来自 `get_prompt("participant_state")`，user 消息包含 `character_state`/`user_state`/`world_state` 文本、`<update_trigger>` 与 JSON `<evidence>` |
| 同一闭包写回状态 | `character_state`、`user_state` | 校验成功后 `{**原状态, **增量}` 合并，返回包含两份完整状态的增量 |

`_message_pair` 的边界（决定何时生产）：以 `trigger` 对应类型（`user`→`HumanMessage`、`reply`→`AIMessage`）的最新消息为必需证据，向前取最近的另一方消息；跳过带工具调用的 `AIMessage`、空文本消息；没有对应新消息时返回空列表，节点返回 `{}`。首轮允许只有用户消息。

## 消费方

| 消费位置 | 读取内容 | 用途 |
| --- | --- | --- |
| participant_state 节点自身 | `update.character_state.model_dump(exclude_unset=True)`、`update.user_state.model_dump(exclude_unset=True)` | 原子合并回 `AgentState` |
| [`agent/utils/state.py`](../../../../agent/utils/state.py) | `location`、`mood`、`body`、`clothing`、`hearing`（角色）/ 前四项（用户） | `prepare_character_state` / `prepare_user_state` 生成 `<character_state>`、`<user_state>` 文本块 |
| draft 节点 | 上述文本块 | 拼入主回复 system prompt |
| [`server/services/agent.py`](../../../../server/services/agent.py) 的 `public_state` | `character_state` 五键、`user_state` 四键 | 向 UI 投影字符串状态，非字符串值被置 `None` |
| 测试 | 模型输出的 `ParticipantStateUpdate` 校验结果 | `tests/test_participant_state.py` 覆盖缺字段、类型错误、多余字段、显式 `null` 与超时重试 |

## 输入输出示例

模型输出（JSON 文本，`location` 未提供则保留旧值，`clothing` 显式 `null` 清除）：

```json
{
  "character_state": {"body": "精力恢复"},
  "user_state": {"clothing": null}
}
```

合并后的状态增量（旧 `character_state = {"location": "客厅", "mood": "平静", "body": "疲惫", "clothing": "衬衫", "hearing": "same_room"}`）：

```python
{
    "character_state": {"location": "客厅", "mood": "平静", "body": "精力恢复",
                        "clothing": "衬衫", "hearing": "same_room"},
    "user_state": {"location": "客厅", "mood": "开心", "body": "健康", "clothing": None},
}
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)
- 兄弟协议：[`../state/README.md`](../state/README.md)、[`../check/README.md`](../check/README.md)
- 生产节点：[`../../node/participant_state/README.md`](../../node/participant_state/README.md)
- 实现依据：[`agent/node/participant_state.py`](../../../../agent/node/participant_state.py)、[`agent/utils/state.py`](../../../../agent/utils/state.py)
- 测试覆盖（静态阅读交叉核对，未在本页重新执行）：`tests/test_participant_state.py` 的 `test_both_states_are_updated_with_missing_preserved_and_explicit_null_cleared`、`test_bad_json_and_partial_envelope_retry_before_atomic_success`、`test_invalid_field_types_and_extra_fields_never_write_either_state`、`test_timeout_exhaustion_and_cancellation`
- 未验证项：字段取值长度与内容质量不在本协议约束范围，由 Prompt 与模型决定。
