"""各节点共用的状态规范化及提示词格式化。"""

from datetime import datetime

# 角色状态字段
_CHARACTER_STATE_KEYS = ("location", "mood", "body", "clothing", "hearing")
# 用户状态字段（无 hearing：感知判断只以角色的听觉范围为依据）
_USER_STATE_KEYS = ("location", "mood", "body", "clothing")

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _time_period(hour: int) -> str:
    """按小时给出中文宽泛时间段。"""
    if 2< hour < 6:
        return "凌晨"
    if 5< hour < 8:
        return "早上"
    if 8< hour < 11:
        return "上午"
    if 11< hour < 13:
        return "中午"
    if 13< hour < 17:
        return "下午"
    if 17< hour < 19:
        return "傍晚"
    if 19< hour < 24:
        return "晚上"
    return "深夜"


def system_world_time() -> dict:
    """世界状态时间字段的系统时间来源。"""
    now = datetime.now()
    return {
        "date": now.strftime("%Y-%m-%d"),
        "weekday": _WEEKDAYS[now.weekday()],
        "period": _time_period(now.hour),
    }


def prepare_world_state(world_state: dict | None) -> tuple[dict, str]:
    """解析外部世界状态：时间总是直接取系统时间，仅天气来自状态数据。"""
    state_data = {
        "time": system_world_time(),
        "weather": None,
    }

    if isinstance(world_state, dict):
        state_data["weather"] = world_state.get("weather")

    state_text = (
        "<world_state>\n"
        f"date: {state_data['time']['date']},\n"
        f"weekday: {state_data['time']['weekday']},\n"
        f"time_period: {state_data['time']['period']},\n"
        f"weather: {state_data['weather']},\n"
        "</world_state>\n"
    )
    return state_data, state_text


def prepare_character_state(character_state: dict | None) -> tuple[dict, str]:
    """解析角色状态，返回 (state_data, state_text)。"""
    return _prepare_participant_state(character_state, _CHARACTER_STATE_KEYS, "character_state")


def prepare_user_state(user_state: dict | None) -> tuple[dict, str]:
    """解析用户状态，返回 (state_data, state_text)。"""
    return _prepare_participant_state(user_state, _USER_STATE_KEYS, "user_state")


def _prepare_participant_state(state: dict | None, keys: tuple[str, ...], tag: str) -> tuple[dict, str]:
    source = state if isinstance(state, dict) else {}
    state_data = {key: source.get(key) for key in keys}

    state_text = (
        f"<{tag}>\n"
        + "".join(f"{key}: {state_data[key]},\n" for key in keys)
        + f"</{tag}>\n"
    )
    return state_data, state_text
