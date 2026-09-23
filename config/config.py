from pathlib import Path
import os

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"

load_dotenv(CONFIG_DIR / ".env", verbose=True)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
KIMI_API_KEY = os.getenv("KIMI_API_KEY")
DB_URL = os.getenv("DB_URL", "")
MODEL_CONTEXT_TOKEN_BUDGET = max(2048, int(os.getenv('MODEL_CONTEXT_TOKEN_BUDGET', '16000')))

# 和风天气实时天气（JWT Ed25519 认证，见 config/.env_example）
QWEATHER_API_HOST = os.getenv("QWEATHER_API_HOST", "")
QWEATHER_KEY_ID = os.getenv("QWEATHER_KEY_ID", "")
QWEATHER_PROJECT_ID = os.getenv("QWEATHER_PROJECT_ID", "")
QWEATHER_DEVELOPER_ID = os.getenv("QWEATHER_DEVELOPER_ID", "")
QWEATHER_PRIVATE_KEY_PATH = os.getenv("QWEATHER_PRIVATE_KEY_PATH", "")
QWEATHER_PRIVATE_KEY = os.getenv("QWEATHER_PRIVATE_KEY", "")
QWEATHER_LATITUDE = os.getenv("QWEATHER_LATITUDE", "")
QWEATHER_LONGITUDE = os.getenv("QWEATHER_LONGITUDE", "")
QWEATHER_LANG = os.getenv("QWEATHER_LANG", "zh")
QWEATHER_CACHE_TTL = os.getenv("QWEATHER_CACHE_TTL", "1200")

BGEV2M3_RERANKER_PATH = os.getenv("BGEV2M3_RERANKER_PATH", "")
QWEN3_RERANKER_PATH = os.getenv("QWEN3_RERANKER_PATH", "")
QWEN3_4BRERANKER_PATH = os.getenv("QWEN3_4BRERANKER_PATH", "")
QWEN3_EMBEDDING_PATH = os.getenv("QWEN3_EMBEDDING_PATH", "")
QWEN3_4BEMBEDDING_PATH = os.getenv("QWEN3_4BEMBEDDING_PATH", "")
LOCAL_GGUF_MODEL_PATH = os.getenv("LOCAL_GGUF_MODEL_PATH","")

QWEN3_TTS_BASE_PATH = os.getenv("Qwen3_TTS_12Hz_1_7B-Base", "")
QWEN3_TTS_CUSTOM_PATH = os.getenv("Qwen3_TTS_12Hz_1_7B-Custom", "")
QWEN3_TTS_VOICEDESIGN_PATH = os.getenv("Qwen3_TTS_12Hz_1_7B-VoiceDesign", "")
TTS_MODEL_TYPE = os.getenv("TTS_MODEL_TYPE", "voice_design")
TTS_SPEAKER_ID = os.getenv("TTS_SPEAKER_ID", "")
TTS_VOICE_REF_PATH = os.getenv(
    "TTS_VOICE_REFERENCE_PATH",
    str(DATA_DIR / "voice" / "Ricca_03_04.hca.wav"),
)

DEFAULT_DOWNLOAD_DIR = os.getenv(
    "DEFAULT_DOWNLOAD_DIR",
    str(DATA_DIR / "downloads"),
)

MINIMUM_ITERATIONS = int(os.getenv("MINIMUM_ITERATIONS", "3"))
MAXIMUM_ITERATIONS = int(os.getenv("MAXIMUM_ITERATIONS", "20"))
MEMORY_TOKEN_THRESHOLD = int(os.getenv("MEMORY_TOKEN_THRESHOLD", "20000"))
