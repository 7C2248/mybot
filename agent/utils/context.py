"""Token estimates shared by context limiting and memory routing."""

import json


def estimate_tokens_from_bytes(text: str) -> int:
    if not text:
        return 0
    data = text.encode('utf-8')
    ascii_bytes = sum(1 for byte in data if byte < 128)
    return max(1, ascii_bytes // 4 + (len(data) - ascii_bytes) // 2)


def estimate_message_tokens(message) -> int:
    text = message.content or ''
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    for call in getattr(message, 'tool_calls', None) or []:
        text += json.dumps(call.get('args') or {}, ensure_ascii=False)
    return estimate_tokens_from_bytes(text)
