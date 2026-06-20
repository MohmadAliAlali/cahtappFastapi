import logging
from contextlib import asynccontextmanager
from typing import Optional

from app.core.config import settings

logger = logging.getLogger("chat.redis")

_pool = None
_redis_module = None


def _ensure_redis():
    global _redis_module
    if _redis_module is None:
        try:
            import redis.asyncio as rai
            _redis_module = rai
        except ImportError:
            logger.warning("redis package not installed. Redis features disabled.")
            return None
    return _redis_module


def _redis_available() -> bool:
    return bool(settings.REDIS_URL) and _ensure_redis() is not None


def get_redis_pool():
    global _pool
    if not _redis_available():
        return None
    if _pool is None:
        rai = _ensure_redis()
        _pool = rai.ConnectionPool.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,
        )
    return _pool


def get_redis():
    if not _redis_available():
        return None
    pool = get_redis_pool()
    if pool is None:
        return None
    rai = _ensure_redis()
    return rai.Redis(connection_pool=pool)


@asynccontextmanager
async def redis_lifespan():
    yield
    global _pool
    if _pool is not None:
        await _pool.disconnect()
        _pool = None


def get_channel_name(conversation_id: str) -> str:
    return f"chat:{conversation_id}"
