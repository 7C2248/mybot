"""Prompt 统一入口：main/ 为主 LLM prompt，tools/ 为工具/辅助节点 prompt。"""

from .main import (
    get_draft_prompt,
)
from .tools import (
    get_check_prompt,
    get_chunk_prompt,
    get_event_judge_prompt,
    get_event_summary_prompt,
    get_keyword_extract_prompt,
    get_memory_query_prompt,
    get_participant_state_prompt,
    get_tts_instruct_prompt,
)


__all__ = [
    "get_check_prompt",
    "get_chunk_prompt",
    "get_draft_prompt",
    "get_event_judge_prompt",
    "get_event_summary_prompt",
    "get_keyword_extract_prompt",
    "get_memory_query_prompt",
    "get_participant_state_prompt",
    "get_prompt",
    "get_tts_instruct_prompt",
]


def get_prompt(prompt_type: str, **kwargs) -> str:
    language = kwargs.get("language", "zh")
    if prompt_type == "event_judge":
        return get_event_judge_prompt(language=language)
    if prompt_type == "event_summary":
        return get_event_summary_prompt(language=language)
    if prompt_type == "memory_query":
        return get_memory_query_prompt(language=language)
    if prompt_type == "participant_state":
        return get_participant_state_prompt(language=language)
    if prompt_type == "check":
        return get_check_prompt(language=language)
    if prompt_type == "draft":
        return get_draft_prompt(character_name=kwargs.get("character_name", ""), language=language)
    if prompt_type == "tts_instruct":
        return get_tts_instruct_prompt(character_file=kwargs.get("character_file", ""))
    raise ValueError(f"Unknown prompt type: {prompt_type}")
