"""Agent 对话消息的共享序列化。"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def format_history(messages: list, include_tools: bool = False) -> str:
    history = "<history_messages>\n"
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            if len(message.tool_calls) == 0:
                history += f"index:{index} Character:{message.content}\n"
            elif include_tools:
                history += f"index:{index} Toolcalls\n"
        elif isinstance(message, HumanMessage):
            history += f"index:{index} User:{message.content}\n"
        elif isinstance(message, ToolMessage) and include_tools:
            history += f"index:{index} ToolUsage:{message.name}\n"
    history += "</history_messages>\n"
    return history
