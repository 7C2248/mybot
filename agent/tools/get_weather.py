# agent/tools/get_weather.py

from langchain_core.tools import tool

@tool
def get_weather(location, date):
    """Get weather of a location, the user should supply the location and date."""
    return "Rain 7~13°C"
