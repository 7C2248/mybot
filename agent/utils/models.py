"""各节点共用的模型工厂；按需导入本地推理依赖并缓存实例。"""

from functools import lru_cache
from pathlib import Path

from config.config import (
    QWEN3_EMBEDDING_PATH,
    BGEV2M3_RERANKER_PATH,
    QWEN3_TTS_BASE_PATH,
    QWEN3_TTS_CUSTOM_PATH,
    QWEN3_TTS_VOICEDESIGN_PATH,
    TTS_MODEL_TYPE,
)
from config.model_config import get_node_config, get_scoped_models
from utils.daily_logger import get_logger

logger = get_logger("models")


@lru_cache(maxsize=1)
def get_kimi_model():
    from langchain_moonshot import ChatMoonshot

    # kimi_k3模型
    return ChatMoonshot(
        base_url='https://api.moonshot.cn/v1',
        model="kimi-k3",
        reasoning_effort="max",
    )

@lru_cache(maxsize=None)
def _get_default_node_model(node: str):
    return _build_chat_model(node)


def get_node_model(node: str):
    """Service tasks cache models within their own snapshot, never across event loops."""
    models = get_scoped_models()
    if models is None:
        return _get_default_node_model(node)
    if node not in models:
        models[node] = _build_chat_model(node)
    return models[node]


# Keep the standalone cache reset API for existing callers.
get_node_model.cache_clear = _get_default_node_model.cache_clear

@lru_cache(maxsize=1)
def get_qwen_tts_model():
    # 本地 TTS 模型（按环境变量选择类型，权重全局只加载一次）
    import torch
    from qwen_tts import Qwen3TTSModel

    model_type = TTS_MODEL_TYPE.strip().lower()
    path_map = {
        "voice_design": QWEN3_TTS_VOICEDESIGN_PATH,
        "custom_voice": QWEN3_TTS_CUSTOM_PATH,
        "base": QWEN3_TTS_BASE_PATH,
    }
    if model_type not in path_map:
        raise ValueError(
            f"[Models] 未知的 TTS_MODEL_TYPE: {TTS_MODEL_TYPE}，可选 {list(path_map)}"
        )
    model_path = path_map[model_type]
    if not model_path or not Path(model_path).exists():
        raise FileNotFoundError(f"[Models] TTS 模型不存在: {model_path}")

    kwargs = {}
    if torch.cuda.is_available():
        kwargs = {"device_map": "cuda", "dtype": torch.bfloat16}
    model = Qwen3TTSModel.from_pretrained(model_path, **kwargs)
    logger.info(f"TTS 模型加载成功 ({model_type}): {model_path}")
    return model


@lru_cache(maxsize=1)
def get_qwen_embedding_model():
    from sentence_transformers import SentenceTransformer

    # 本地编码模型
    if not QWEN3_EMBEDDING_PATH or not Path(QWEN3_EMBEDDING_PATH).exists():
        raise FileNotFoundError(
            f"[Models] Embedding 模型不存在: {QWEN3_EMBEDDING_PATH}"
        )
    encoding_model = SentenceTransformer(QWEN3_EMBEDDING_PATH)
    logger.info(f"Embedding 模型加载成功: {QWEN3_EMBEDDING_PATH}")
    return encoding_model

@lru_cache(maxsize=1)
def _get_reranker_cross_encoder():
    from langchain_community.cross_encoders import HuggingFaceCrossEncoder

    # 本地 Reranker 交叉编码器（权重全局只加载一次）
    if not BGEV2M3_RERANKER_PATH or not Path(BGEV2M3_RERANKER_PATH).exists():
        raise FileNotFoundError(
            f"[Models] Reranker 模型不存在: {BGEV2M3_RERANKER_PATH}"
        )
    rerank_model = HuggingFaceCrossEncoder(
        model_name=BGEV2M3_RERANKER_PATH,
        model_kwargs={"device": "cuda"},
    )
    logger.info(f"Reranker 模型加载成功: {BGEV2M3_RERANKER_PATH}")
    return rerank_model

@lru_cache(maxsize=8)
def get_reranker_model(top_k: int = 15):
    # Reranker 包装器（按 top_k 缓存，共享同一份模型权重）
    from agent.classes.reranker import CrossEncoderReranker
    return CrossEncoderReranker(model=_get_reranker_cross_encoder(), top_k=top_k)


def _normalize_effort(effort) -> str | None:
    if effort is None:
        return None
    if str(effort).strip().lower() == "none":
        return None
    return str(effort)


def _build_chat_model(node: str):
    """按节点配置构建 LangChain ChatModel。"""
    config = get_node_config(node)
    provider = str(config.get("provider", "deepseek")).strip().lower()
    thinking = str(config.get("thinking", "enabled")).strip().lower() == "enabled"
    effort = _normalize_effort(config.get("reasoning_effort"))

    if provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        kwargs = {
            "model": config["model"],
            "extra_body": {
                "thinking": {"type": "enabled" if thinking else "disabled"}
            },
        }
        if thinking:
            kwargs["reasoning_effort"] = effort or "max"
            # 思考模式不支持强制 tool_choice，结构化输出只能由模型自行选择工具。
            kwargs["disabled_params"] = {"tool_choice": None}
        return ChatDeepSeek(**kwargs)

    if provider == "moonshot":
        from langchain_moonshot import ChatMoonshot

        return ChatMoonshot(
            base_url=config.get("base_url", "https://api.moonshot.cn/v1"),
            model=config["model"],
            reasoning_effort=effort or "max",
        )

    if provider == "llama_cpp":
        from langchain_community.chat_models import ChatLlamaCpp

        return ChatLlamaCpp(
            model_path=config["model_path"],
            n_ctx=config.get("n_ctx", 4096),
            temperature=config.get("temperature"),
        )

    raise ValueError(f"[ModelConfig] 未知 provider: {provider}")


def load_reranker(model_path: str = BGEV2M3_RERANKER_PATH, top_k: int = 4):
    """
    从本地路径加载 Cross-Encoder Reranker。
    """
    if not model_path or not Path(model_path).exists():
        raise FileNotFoundError(
            f"[Reranker] Reranker 模型不存在: {model_path}\n"
        )

    import torch
    from agent.classes.reranker import CrossEncoderReranker
    from langchain_community.cross_encoders import HuggingFaceCrossEncoder

    return CrossEncoderReranker(
        model=HuggingFaceCrossEncoder(
            model_name=model_path,
            model_kwargs={"device": "cuda", "torch_dtype": torch.float16},
        ),
        top_k=top_k,
    )
