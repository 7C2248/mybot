"""Bound model context independently from durable history and memory writes."""
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from agent.utils.context import estimate_message_tokens

__all__ = ['limit_context']


def limit_context(state):
    if 'memory_policy_version' not in state:
        return {}
    messages = list(state.get('messages', []))
    remove = set()
    if not state.get('memory_retrieval_enabled', False):
        hidden_calls = set()
        for message in messages:
            if isinstance(message, AIMessage) and any(c.get('name') == 'memory_query' for c in message.tool_calls):
                remove.add(message.id)
                hidden_calls.update(c['id'] for c in message.tool_calls)
            if isinstance(message, ToolMessage) and (message.tool_call_id in hidden_calls or message.name == 'memory_query'):
                remove.add(message.id)
    remaining = [m for m in messages if m.id not in remove]
    # Cut only before a user turn so tool requests and results stay together.
    from config.config import MODEL_CONTEXT_TOKEN_BUDGET
    starts = [i for i, m in enumerate(remaining) if isinstance(m, HumanMessage)]
    budget = sum(estimate_message_tokens(m) for m in remaining)
    cut = 0
    for start in starts[1:]:
        if budget <= MODEL_CONTEXT_TOKEN_BUDGET and len(remaining) - cut <= 120:
            break
        budget -= sum(estimate_message_tokens(m) for m in remaining[cut:start])
        cut = start
    remove.update(m.id for m in remaining[:cut])
    return {'messages': [RemoveMessage(id=m.id) for m in messages if m.id in remove]} if remove else {}
