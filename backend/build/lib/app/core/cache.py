"""Small Redis-backed JSON cache. Degrades to a no-op if Redis is unset or unreachable:
a cache outage must slow requests down, never fail them."""
import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.logging import get_logger
from app.core.redis import get_async_redis

log = get_logger("cache")
PREFIX = "ims:cache:"


async def get_json(key: str) -> Any | None:
    client = get_async_redis()
    if client is None:
        return None
    try:
        raw = await client.get(PREFIX + key)
    except Exception as exc:  # noqa: BLE001 - cache errors are non-fatal by design
        log.warning("cache_get_failed", key=key, error=str(exc))
        return None
    return json.loads(raw) if raw else None


async def set_json(key: str, value: Any, ttl_seconds: int) -> None:
    client = get_async_redis()
    if client is None:
        return
    try:
        await client.set(PREFIX + key, json.dumps(value, default=str), ex=ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_set_failed", key=key, error=str(exc))


async def cached(key: str, ttl_seconds: int, compute: Callable[[], Awaitable[Any]]) -> tuple[Any, bool]:
    """Return ``(value, hit)``."""
    hit = await get_json(key)
    if hit is not None:
        return hit, True
    value = await compute()
    await set_json(key, value, ttl_seconds)
    return value, False


async def bump_version(namespace: str) -> None:
    """Invalidate every key built with ``versioned_key(namespace, ...)``."""
    client = get_async_redis()
    if client is None:
        return
    try:
        await client.incr(f"{PREFIX}v:{namespace}")
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_bump_failed", namespace=namespace, error=str(exc))


async def versioned_key(namespace: str, *parts: str) -> str:
    client = get_async_redis()
    version = "0"
    if client is not None:
        try:
            raw = await client.get(f"{PREFIX}v:{namespace}")
            version = raw.decode() if raw else "0"
        except Exception:  # noqa: BLE001
            version = "0"
    return ":".join((namespace, version, *parts))
