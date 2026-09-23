"""世界状态更新节点：时间直接取系统时间，天气取和风天气实时数据。"""

from agent.utils.state import prepare_world_state
from agent.utils.weather import fetch_current_weather
from agent.classes.state import AgentState
from utils.daily_logger import get_logger

__all__ = ['update_world_state']

logger = get_logger("node.world_state")


async def update_world_state(state: AgentState):
    world_state, _ = prepare_world_state(state.get("world_state"))
    weather = await fetch_current_weather()
    if weather is not None and weather != world_state.get("weather"):
        world_state["weather"] = weather
        logger.info(f"world weather updated: {weather}")
    return {"world_state": world_state}
