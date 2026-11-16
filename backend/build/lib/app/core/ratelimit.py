"""Application-level rate limiting (in addition to the nginx edge limits).

Counters live in Redis when ``REDIS_URL`` is set, so limits hold across API replicas;
otherwise they fall back to process memory (single-process dev only). A Redis outage
fails *open* and is logged: rate limiting must never take authentication down.

Usage::

    @router.post("/login", dependencies=[Depends(rate_limit("login", "login_rate_limit"))])
"""
import time
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request
from limits import RateLimitItem, parse
from limits.aio.storage import MemoryStorage, RedisStorage, Storage
from limits.aio.strategies import MovingWindowRateLimiter

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("ratelimit")

_storage: Storage | None = None
_limiter: MovingWindowRateLimiter | None = None


def _get_limiter() -> MovingWindowRateLimiter:
    global _storage, _limiter
    if _limiter is None:
        url = get_settings().redis_url
        if url:
            _storage = RedisStorage(
                url.replace("redis://", "async+redis://", 1).replace("rediss://", "async+rediss://", 1),
                implementation="redispy",
                key_prefix="ims:rl",
                wrap_exceptions=True,
            )
        else:
            _storage = MemoryStorage()
        _limiter = MovingWindowRateLimiter(_storage)
    return _limiter


async def reset() -> None:
    """Clear all counters (tests)."""
    global _storage, _limiter
    if _storage is not None:
        try:
            await _storage.reset()
        except Exception as exc:  # noqa: BLE001 - best effort
            log.debug("ratelimit_reset_failed", error=str(exc))
    _storage = None
    _limiter = None


def client_ip(request: Request) -> str:
    # uvicorn --proxy-headers rewrites request.client from X-Forwarded-For, but only for
    # proxies listed in FORWARDED_ALLOW_IPS, so this cannot be spoofed by clients.
    return request.client.host if request.client else "unknown"


async def check(bucket: str, limit: str | RateLimitItem, key: str) -> None:
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return
    item = parse(limit) if isinstance(limit, str) else limit
    limiter = _get_limiter()
    try:
        allowed = await limiter.hit(item, bucket, key)
        if allowed:
            return
        stats = await limiter.get_window_stats(item, bucket, key)
        retry_after = max(1, int(stats.reset_time - time.time()))
    except Exception as exc:  # noqa: BLE001 - fail open, see module docstring
        log.warning("ratelimit_backend_error", bucket=bucket, error=str(exc))
        return
    log.info("ratelimit_exceeded", bucket=bucket, key=key, limit=str(item))
    raise HTTPException(
        status_code=429,
        detail="Too many requests. Please wait and try again.",
        headers={"Retry-After": str(retry_after)},
    )


def rate_limit(
    bucket: str,
    setting_name: str,
    key_func: Callable[[Request], str] = client_ip,
) -> Callable[[Request], Any]:
    async def dependency(request: Request) -> None:
        limit = getattr(get_settings(), setting_name)
        await check(bucket, limit, key_func(request))

    return dependency
