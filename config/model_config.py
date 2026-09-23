# config/model_config.py
"""读取 models.yaml 并合并各节点配置；模型构建由 agent.utils.models 负责。"""

from functools import lru_cache
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar

import yaml

_MODEL_CONFIG_PATH = Path(__file__).resolve().parent / "models.yaml"
_ACTIVE_CONFIG = ContextVar("mybot_model_configuration", default=None)
_ACTIVE_MODELS = ContextVar('mybot_task_models', default=None)


@contextmanager
def model_config_scope(data: dict):
    """Service tasks use a configuration snapshot and their own node-model cache."""
    token = _ACTIVE_CONFIG.set(data)
    models_token = _ACTIVE_MODELS.set({})
    try:
        yield
    finally:
        _ACTIVE_MODELS.reset(models_token)
        _ACTIVE_CONFIG.reset(token)


def get_scoped_models():
    """Models belong to one configuration snapshot and its execution context."""
    return _ACTIVE_MODELS.get()


@lru_cache(maxsize=1)
def _load_model_config() -> dict:
    if not _MODEL_CONFIG_PATH.exists():
        raise FileNotFoundError(f"[ModelConfig] 配置文件不存在: {_MODEL_CONFIG_PATH}")
    with open(_MODEL_CONFIG_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict):
        raise ValueError("[ModelConfig] models.yaml 缺少 nodes 配置")
    return data


def get_node_config(node: str) -> dict:
    """返回节点合并 defaults 后的完整配置。"""
    data = _ACTIVE_CONFIG.get()
    if data is None:
        data = _load_model_config()
    nodes = data["nodes"]
    if node not in nodes:
        raise KeyError(f"[ModelConfig] 未知节点: {node}，可用节点: {list(nodes)}")
    merged = dict(data.get("defaults") or {})
    merged.update(nodes[node] or {})
    return merged
