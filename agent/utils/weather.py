"""和风天气实时天气客户端：Ed25519 JWT 认证、结果缓存与失败降级。

仅在配置齐全时请求；任何失败都返回 None，由调用方保留旧天气。
接口参考：https://dev.qweather.com/docs/api/weather/weather-current/
"""

import asyncio
import time
from pathlib import Path

from config.config import (
    PROJECT_ROOT,
    QWEATHER_API_HOST,
    QWEATHER_CACHE_TTL,
    QWEATHER_DEVELOPER_ID,
    QWEATHER_KEY_ID,
    QWEATHER_LANG,
    QWEATHER_LATITUDE,
    QWEATHER_LONGITUDE,
    QWEATHER_PRIVATE_KEY,
    QWEATHER_PRIVATE_KEY_PATH,
    QWEATHER_PROJECT_ID,
)
from utils.daily_logger import get_logger

__all__ = ['fetch_current_weather']

logger = get_logger("utils.weather")

_DEFAULT_CACHE_TTL = 1200.0     # 实时天气缓存时间（秒）
_REQUEST_TIMEOUT = 10.0         # 单次 HTTP 请求超时（秒）
_JWT_LIFETIME = 900             # JWT 有效期（秒），和风天气最长支持 24 小时
_JWT_REFRESH_MARGIN = 60        # JWT 提前刷新余量（秒）

_token_cache = {"value": "", "expires_at": 0.0}
_weather_cache = {"value": None, "expires_at": 0.0}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _cache_ttl() -> float:
    try:
        ttl = float(QWEATHER_CACHE_TTL)
    except (TypeError, ValueError):
        return _DEFAULT_CACHE_TTL
    return ttl if ttl > 0 else _DEFAULT_CACHE_TTL


def _api_host() -> str:
    host = _text(QWEATHER_API_HOST).rstrip("/")
    if host and "://" not in host:
        host = "https://" + host
    return host


def _private_key() -> str:
    path = _text(QWEATHER_PRIVATE_KEY_PATH)
    if path:
        key_path = Path(path)
        if not key_path.is_absolute():
            key_path = PROJECT_ROOT / key_path
        return key_path.read_text(encoding="utf-8")
    return _text(QWEATHER_PRIVATE_KEY).replace("\\n", "\n")


def _configured() -> bool:
    if not all((_api_host(), _text(QWEATHER_LATITUDE), _text(QWEATHER_LONGITUDE),
                _text(QWEATHER_KEY_ID), _text(QWEATHER_PROJECT_ID),
                _text(QWEATHER_DEVELOPER_ID))):
        return False
    try:
        return bool(_private_key())
    except OSError as exc:
        logger.warning("天气私钥不可读: %s", exc)
        return False


def _token() -> str:
    """生成并缓存 Ed25519 JWT；过期前 60 秒自动刷新。"""
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]

    import jwt

    issued_at = int(now) - 30
    token = jwt.encode(
        {"iss": _text(QWEATHER_DEVELOPER_ID), "sub": _text(QWEATHER_PROJECT_ID),
         "iat": issued_at, "exp": issued_at + _JWT_LIFETIME},
        _private_key(),
        algorithm="EdDSA",
        headers={"kid": _text(QWEATHER_KEY_ID)},
    )
    _token_cache.update(value=token, expires_at=now + _JWT_LIFETIME - _JWT_REFRESH_MARGIN)
    return token


def _request_weather() -> str | None:
    import requests

    response = requests.get(
        f"{_api_host()}/weather/v1/current/{_text(QWEATHER_LATITUDE)}/{_text(QWEATHER_LONGITUDE)}",
        headers={"Authorization": f"Bearer {_token()}"},
        params={"lang": _text(QWEATHER_LANG) or "zh"},
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    condition = payload.get("condition") if isinstance(payload, dict) else None
    text = _text((condition or {}).get("text"))
    if not text:
        raise ValueError(f"响应缺少天气现象: {payload}")

    temperature = (payload.get("temperature") or {}).get("value")
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        return f"{text} {round(temperature)}°C"
    return text


async def fetch_current_weather() -> str | None:
    """返回当前天气文本（如 "少云 32°C"）；未配置或请求失败时返回 None。"""
    now = time.monotonic()
    if _weather_cache["value"] is not None and now < _weather_cache["expires_at"]:
        return _weather_cache["value"]
    if not _configured():
        return None

    try:
        weather = await asyncio.to_thread(_request_weather)
    except Exception as exc:
        logger.warning("实时天气请求失败: %s: %s", type(exc).__name__, exc)
        return None
    if weather is None:
        return None

    _weather_cache.update(value=weather, expires_at=now + _cache_ttl())
    logger.info("实时天气已更新: %s", weather)
    return weather
