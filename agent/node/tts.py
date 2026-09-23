# agent/node/tts.py

import asyncio
import json
import re
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.utils.models import get_node_model, get_qwen_tts_model
from agent.prompts import get_prompt
from config.config import TTS_MODEL_TYPE, TTS_SPEAKER_ID, TTS_VOICE_REF_PATH
from agent.utils.text import strip_code_fence, strip_timestamps
from utils.daily_logger import get_logger

__all__ = ['create_tts_node']

logger = get_logger("node.tts")

_DEFAULT_PAUSE = 0.3
_MAX_PAUSE = 3.0


################# 回复文本解析 ####################

def _parse_reply(content: str) -> list[dict]:
    """解析角色回复，提取对白文本与对应的动作描述。

    规则（与主节点提示词的正文格式对应）:
    - 「」设备消息不朗读，直接丢弃
    - （）动作/旁白描述作为描述保留，不朗读
    - 其余纯文本视为对白
    - 纯动作行（无对白）的描述并入下一段对白
    """
    content = strip_timestamps(content)
    segments = []
    pending_descriptions = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"「.*?」", "", line)
        descriptions = re.findall(r"（(.*?)）", line)
        line = re.sub(r"（.*?）", "", line).strip()
        if not line:
            pending_descriptions.extend(descriptions)
            continue
        descriptions = pending_descriptions + descriptions
        pending_descriptions = []
        text = re.sub(r"♡", "", line).strip()
        if text:
            segments.append({"text": text, "description": " ".join(descriptions)})
    return segments


################# 角色档案读取 ####################

def _load_voice_profile(character_name: str) -> str:
    """加载角色 TTS 专用语音档案，缺失时回退到原始角色档案。"""
    if not character_name:
        return ""
    from agent.utils.character import character_dir
    base = character_dir(character_name)
    tts_path = base / "tts.md"
    if tts_path.exists():
        return tts_path.read_text(encoding="utf-8")
    for filename in ("profile_cn.md", "profile_en.md"):
        path = base / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
    candidates = sorted(base.glob("profile_*.md"))
    return candidates[0].read_text(encoding="utf-8") if candidates else ""


################# 语气指令与停顿生成 ####################

def _parse_plans(content: str, count: int) -> list[dict]:
    """解析 LLM 输出为 [{instruct, pause}, ...]，与对白数量对齐。"""
    content = (content or "").strip()
    content = strip_code_fence(content)

    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        logger.warning(f"指令解析失败，使用默认值: {content!r}")
        return [{"instruct": "", "pause": _DEFAULT_PAUSE} for _ in range(count)]

    plans = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            plans.append({"instruct": "", "pause": _DEFAULT_PAUSE})
            continue
        instruct = str(item.get("instruct", "") or "").strip()
        try:
            pause = float(item.get("pause", _DEFAULT_PAUSE))
        except (TypeError, ValueError):
            pause = _DEFAULT_PAUSE
        plans.append({
            "instruct": instruct,
            "pause": max(0.0, min(_MAX_PAUSE, pause)),
        })
    while len(plans) < count:
        plans.append({"instruct": "", "pause": _DEFAULT_PAUSE})
    return plans[:count]


async def _generate_segment_plans(segments: list[dict], character_file: str) -> list[dict]:
    """通过全局 LLM 为每一句对白生成语气提示词与句间停顿。"""
    llm = get_node_model("tts")
    numbered = "\n".join(
        f"{index + 1}. 对白：{segment['text']} | 描述：{segment['description'] or '无'}"
        for index, segment in enumerate(segments)
    )
    response = await llm.ainvoke([
        SystemMessage(content=get_prompt("tts_instruct", character_file=character_file)),
        HumanMessage(content=numbered),
    ])
    content = (getattr(response, "content", "") or "").strip()
    if not content:
        reasoning = response.additional_kwargs.get("reasoning_content", "")
        m = re.search(r"</thinking_process>", reasoning)
        if m:
            content = reasoning[m.end():].strip()
    return _parse_plans(content, len(segments))


################# 播放 ####################

def _play_audio(waveforms: list, sample_rate: int, pauses: list):
    """按段播放，段间按 LLM 回传的停顿间隔休眠。"""
    if not waveforms:
        return
    for i, wav in enumerate(waveforms):
        sd.play(np.asarray(wav, dtype=np.float32), sample_rate)
        sd.wait()
        if i < len(pauses) and pauses[i] > 0:
            time.sleep(pauses[i])


################# TTS 节点构建 ####################

def create_tts_node(character_name: str, *, audio_sink=None, character_file: str | None = None):
    """按环境变量初始化 TTS 节点，模型权重延迟到首次调用时加载。"""
    model_type = TTS_MODEL_TYPE.strip().lower()
    if model_type not in ("voice_design", "custom_voice", "base"):
        raise ValueError(f"[TTS] 未知的 TTS_MODEL_TYPE: {TTS_MODEL_TYPE}")
    character_file = character_file if character_file is not None else _load_voice_profile(character_name)

    cache = {"model": None, "speaker": None, "prompt_items": None}

    def _ensure_model():
        if cache["model"] is not None:
            return
        model = get_qwen_tts_model()
        if model_type == "custom_voice":
            speakers = model.get_supported_speakers() or []
            speaker = TTS_SPEAKER_ID.strip() or (speakers[0] if speakers else "")
            if not speaker:
                raise ValueError("[TTS] CustomVoice 未配置可用音色")
            logger.info(f"CustomVoice 使用音色: {speaker}")
            cache["speaker"] = speaker
        elif model_type == "base":
            cache["prompt_items"] = model.create_voice_clone_prompt(
                ref_audio=TTS_VOICE_REF_PATH, x_vector_only_mode=True
            )
        cache["model"] = model

    def synthesize(texts, instructions):
        _ensure_model()
        model = cache["model"]
        if model_type == "voice_design":
            return model.generate_voice_design(
                texts, instruct=instructions, language="Auto"
            )
        if model_type == "custom_voice":
            return model.generate_custom_voice(
                texts, speaker=cache["speaker"], instruct=instructions, language="Auto"
            )
        return model.generate_voice_clone(
            texts, language="Auto", voice_clone_prompt=cache["prompt_items"]
        )

    def synthesize_and_play(texts, instructions, pauses):
        waveforms, sample_rate = synthesize(texts, instructions)
        if audio_sink is not None:
            audio_sink(waveforms, sample_rate, pauses)
        else:
            _play_audio(waveforms, sample_rate, pauses)

    async def tts_node(state):
        if not state.get("need_tts", False):
            return {}
        messages = state.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return {}
        content = messages[-1].content or ""
        segments = [segment for segment in _parse_reply(content) if segment["text"]]
        texts = [segment["text"] for segment in segments]
        if not texts:
            if audio_sink is not None:
                raise ValueError("speech_empty")
            logger.info("回复中无对白，跳过语音生成")
            return {}

        try:
            plans = await _generate_segment_plans(segments, character_file)
        except Exception as e:
            logger.warning(f"语气指令生成失败: {e}")
            plans = []
        if not plans:
            plans = [{"instruct": "", "pause": _DEFAULT_PAUSE} for _ in texts]
        instructions = [plan["instruct"] for plan in plans]
        pauses = [plan["pause"] for plan in plans]
        logger.info(f"合成 {len(texts)} 段对白 | 指令: {instructions!r} | 停顿: {pauses!r}")

        try:
            await asyncio.to_thread(synthesize_and_play, texts, instructions, pauses)
        except Exception as e:
            if audio_sink is not None:
                raise
            logger.warning(f"语音生成/播放失败: {e}")
        return {}

    return tts_node
