"""RQ entry points. Each wraps an async runner from ``app.services.jobs``.

Run a worker with::

    rq worker default --url "$REDIS_URL"
"""
import asyncio
from collections.abc import Coroutine
from typing import Any

from app.core.config import get_settings
from app.core.logging import configure_logging


def _run(coro: Coroutine[Any, Any, None]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    asyncio.run(coro)


def run_export(job_id: str) -> None:
    from app.services.jobs import run_export as runner

    _run(runner(job_id))


def run_import(job_id: str) -> None:
    from app.services.jobs import run_import as runner

    _run(runner(job_id))


def fanout_announcement(announcement_id: str) -> None:
    from app.services.jobs import fanout_announcement as runner

    _run(runner(announcement_id))
