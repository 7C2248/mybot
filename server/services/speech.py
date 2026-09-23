"""Generate browser-playable WAV files without playing audio on the service host."""

import os
from pathlib import Path
import tempfile
import wave

from server.classes.api import ServiceError


def audio_path(root: Path, resource_id) -> Path:
    base = root.resolve()
    path = base / f"{resource_id}.wav"
    if not path.resolve().is_relative_to(base) or path.is_symlink():
        raise ServiceError("resource_not_found", "音频资源不存在。", 404)
    return path


def write_wave(path: Path, waveforms, sample_rate, pauses):
    import numpy as np
    if not 8000 <= sample_rate <= 192000 or not waveforms:
        raise ValueError("Invalid synthesized audio")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(suffix=".wav.tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                for index, samples in enumerate(waveforms):
                    samples = np.asarray(samples, dtype=np.float32)
                    if samples.ndim != 1 or not np.isfinite(samples).all():
                        raise ValueError("Invalid waveform")
                    wav.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
                    if index < len(waveforms) - 1:
                        pause = max(0, min(3, float(pauses[index])))
                        wav.writeframes(b"\0\0" * int(sample_rate * pause))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class SpeechService:
    def __init__(self, root, catalog, models):
        self.root, self.catalog, self.models = root, catalog, models

    async def execute(self, job):
        from langchain_core.messages import AIMessage
        from agent.node.tts import create_tts_node
        from config.model_config import model_config_scope
        text = self.catalog.voice_profile(job["character_id"])
        target = audio_path(self.root, job["id"])
        data, _ = self.models.snapshot()
        with model_config_scope(data):
            node = create_tts_node(job["character_id"], character_file=text,
                                   audio_sink=lambda waveforms, rate, pauses: write_wave(target, waveforms, rate, pauses))
            await node({"need_tts": True, "messages": [AIMessage(content=job["text"])]})
