"""Shared Redis clients (async for the API, sync for RQ). Absent when REDIS_URL is unset."""
from typing import Any

from app.core.config import get_settings

_async_client: Any = None
_sync_client: Any = None


def redis_enabled() -> bool:
    return bool(get_settings().redis_url)


def get_async_redis() -> Any:
    global _async_client
    if _async_client is None and redis_enabled():
        from redis import asyncio as aioredis

        _async_client = aioredis.from_url(
            get_settings().redis_url,
            socket_timeout=2,
            socket_connect_timeout=2,
            health_check_interval=30,
        )
    return _async_client


def get_sync_redis() -> Any:
    global _sync_client
    if _sync_client is None and redis_enabled():
        import redis

        _sync_client = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2)
    return _sync_client


async def close_redis() -> None:
    global _async_client
    if _async_client is not None:
        await _async_client.aclose()
        _async_client = None
