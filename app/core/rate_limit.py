import logging
import os
import time
from functools import wraps
from typing import Callable, Optional

from fastapi import HTTPException, Request, status

from app.core.config import settings
from app.core.redis_client import get_redis

logger = logging.getLogger("chat.ratelimit")


class MemoryRateLimiter:
    def __init__(self):
        self._counts: dict[str, list[float]] = {}
        self._last_prune = time.time()

    async def is_allowed(
        self,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> tuple[bool, int]:
        now = time.time()

        # Prune expired entries periodically (every 60 seconds)
        if now - self._last_prune > 60:
            self._prune(now, window_seconds)
            self._last_prune = now

        timestamps = self._counts.get(key, [])
        timestamps = [t for t in timestamps if now - t < window_seconds]
        if len(timestamps) >= limit:
            self._counts[key] = timestamps
            return False, len(timestamps)
        timestamps.append(now)
        self._counts[key] = timestamps
        return True, len(timestamps)

    def _prune(self, now: float, window_seconds: int) -> None:
        expired = [
            key for key, timestamps in self._counts.items()
            if not any(now - t < window_seconds for t in timestamps)
        ]
        for key in expired:
            del self._counts[key]


class RedisRateLimiter:
    def __init__(self, redis):
        self.redis = redis

    async def is_allowed(
        self,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> tuple[bool, int]:
        now = int(time.time())
        pipeline = self.redis.pipeline()
        pipeline.zremrangebyscore(key, 0, now - window_seconds)
        pipeline.zcard(key)
        pipeline.zadd(key, {f"{now}:{time.time_ns()}": now})
        pipeline.expire(key, window_seconds)
        results = await pipeline.execute()
        current_count = results[1]
        if current_count >= limit:
            return False, current_count
        return True, current_count + 1


_rate_limiter: Optional[MemoryRateLimiter | RedisRateLimiter] = None


def get_rate_limiter():
    global _rate_limiter
    if _rate_limiter is None:
        redis = get_redis()
        if redis is not None:
            _rate_limiter = RedisRateLimiter(redis)
            logger.info("Using Redis rate limiter")
        else:
            _rate_limiter = MemoryRateLimiter()
            logger.info("Using in-memory rate limiter (no Redis)")
    return _rate_limiter


def rate_limit(
    limit: int = 10,
    window_seconds: int = 60,
    key_prefix: str = "ratelimit",
):
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if settings.DISABLE_RATE_LIMIT:
                return await func(*args, **kwargs)
            request: Request = kwargs.get("request") or next(
                (a for a in args if isinstance(a, Request)), None
            )
            if request is None:
                return await func(*args, **kwargs)

            client_ip = request.client.host if request.client else "unknown"
            key = f"{key_prefix}:{client_ip}:{func.__name__}"

            limiter = get_rate_limiter()
            allowed, count = await limiter.is_allowed(key, limit, window_seconds)

            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Rate limit exceeded. Try again in {window_seconds} seconds.",
                    headers={"Retry-After": str(window_seconds)},
                )

            return await func(*args, **kwargs)

        return wrapper
    return decorator
