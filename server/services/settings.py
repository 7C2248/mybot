"""Report configuration presence, never connection strings, keys or model parameters."""

from pathlib import Path

import yaml

from server.classes.api import Capabilities, ModelStatus, SettingsStatus
from server.config import ServiceSettings

_PUBLIC_NODES = ("main", "participant_state", "memory_query", "memory_summary", "chunking")


def read_settings(settings: ServiceSettings, *, database_ready: bool) -> SettingsStatus:
    models = {}
    config_status = "missing"
    try:
        data = yaml.safe_load(settings.model_config.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict):
            raise ValueError("Invalid model configuration")
        defaults = data.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise ValueError("Invalid model defaults")
        config_status = "present"
        for name in _PUBLIC_NODES:
            if name not in data["nodes"]:
                continue
            node = data["nodes"][name] or {}
            if not isinstance(node, dict):
                raise ValueError("Invalid model node")
            config = defaults | node
            provider = config.get("provider")
            if provider not in ("deepseek", "moonshot", "llama_cpp"):
                provider = "unknown"
            credentials = None
            local_available = None
            if provider in ("deepseek", "moonshot"):
                key = "DEEPSEEK_API_KEY" if provider == "deepseek" else "KIMI_API_KEY"
                credentials = bool(settings.environment.get(key))
            if provider == "llama_cpp":
                path = settings.environment.get("LOCAL_GGUF_MODEL_PATH", "")
                local_available = bool(path) and Path(path).is_file()
            models[name] = ModelStatus(
                provider=provider, configured=bool(config.get("model")) or bool(local_available),
                credentials_configured=credentials, local_model_available=local_available,
            )
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        config_status = "invalid"
        models = {}
    return SettingsStatus(
        model_configuration=config_status, models=models, database_configured=bool(settings.db_url),
        capabilities=Capabilities(memories=database_ready),
    )
