"""Background job dispatch and runners.

Runners are plain coroutines that open their own DB session, so they behave the same
whether they run in an RQ worker process (``app.workers.tasks``) or inline in the API
process (dev / tests, ``JOB_BACKEND=inline``). Inline mode awaits the runner before
responding, which is fine for development but must not be used for large production
workloads.
"""
import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.job import Job, JobStatus
from app.models.user import Role, User

log = get_logger("jobs")

FANOUT_BATCH = 500

# Tests (and anything else needing a different database) can replace this.
session_factory: Callable[[], AsyncSession] | None = None


@asynccontextmanager
async def _open_session() -> AsyncIterator[AsyncSession]:
    if session_factory is not None:
        async with session_factory() as session:
            yield session
        return
    # NullPool + per-job engine: an RQ job runs inside its own asyncio.run() loop, and
    # pooled asyncpg connections cannot be shared across loops.
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


async def create_job(
    session: AsyncSession,
    user: User,
    kind: str,
    params: dict[str, Any],
    organization_id: str | None,
) -> Job:
    job = Job(
        user_id=user.id,
        organization_id=organization_id,
        kind=kind,
        params_json=json.dumps(params, default=str),
        status=JobStatus.QUEUED,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


def jobs_query(user: User, kind: str | None = None) -> Select[Any]:
    stmt = select(Job)
    if user.role != Role.SUPER_ADMIN:
        stmt = stmt.where(Job.user_id == user.id)
    if kind:
        stmt = stmt.where(Job.kind == kind)
    return stmt


async def get_job_for(session: AsyncSession, user: User, job_id: str) -> Job:
    from app.services.access import not_found

    job = await session.get(Job, job_id)
    if job is None or (user.role != Role.SUPER_ADMIN and job.user_id != user.id):
        raise not_found("Job")
    return job


# --- dispatch -----------------------------------------------------------------


async def enqueue(task_name: str, *args: Any) -> None:
    settings = get_settings()
    if settings.effective_job_backend == "rq":
        from rq import Queue, Retry

        from app.core.redis import get_sync_redis

        def _push() -> None:
            queue = Queue(settings.job_queue, connection=get_sync_redis())
            queue.enqueue(
                f"app.workers.tasks.{task_name}",
                *args,
                job_timeout=settings.job_timeout_seconds,
                retry=Retry(max=2, interval=[10, 60]),
                failure_ttl=7 * 24 * 3600,
            )

        await asyncio.to_thread(_push)
        log.info("job_enqueued", task=task_name, backend="rq")
        return
    log.info("job_inline", task=task_name)
    await RUNNERS[task_name](*args)


async def enqueue_announcement_fanout(announcement_id: str) -> None:
    await enqueue("fanout_announcement", announcement_id)


# --- runner helpers --------------------------------------------------------------


async def _mark(session: AsyncSession, job: Job, **fields: Any) -> None:
    for name, value in fields.items():
        setattr(job, name, value)
    await session.commit()


async def _run_tracked(job_id: str, body: Callable[[AsyncSession, Job], Awaitable[None]]) -> None:
    async with _open_session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            log.warning("job_missing", job_id=job_id)
            return
        if job.status == JobStatus.SUCCEEDED:
            return  # RQ retry after a success that was not acknowledged
        await _mark(session, job, status=JobStatus.RUNNING, started_at=datetime.now(UTC), error="")
        try:
            await body(session, job)
        except Exception as exc:
            await session.rollback()
            job = await session.get(Job, job_id)
            if job is not None:
                await _mark(
                    session,
                    job,
                    status=JobStatus.FAILED,
                    error=str(exc)[:2000] or exc.__class__.__name__,
                    finished_at=datetime.now(UTC),
                )
            log.exception("job_failed", job_id=job_id, kind=getattr(job, "kind", None))
            return
        await _mark(session, job, status=JobStatus.SUCCEEDED, progress=100, finished_at=datetime.now(UTC))
        log.info("job_succeeded", job_id=job_id, kind=job.kind)


async def _notify(session: AsyncSession, user_id: str, title: str, body: str) -> None:
    from app.models.engagement import Notification

    session.add(Notification(user_id=user_id, title=title, body=body))
    await session.commit()


# --- runners ------------------------------------------------------------------------


async def run_export(job_id: str) -> None:
    async def body(session: AsyncSession, job: Job) -> None:
        from app.services import export_service
        from app.utils.storage import get_storage

        params = json.loads(job.params_json)
        cohort, data = await export_service.get_cohort_export_data(session, params["cohort_id"])
        fmt = params["format"]
        content, filename, content_type = await asyncio.to_thread(
            export_service.render, data, cohort, fmt, params.get("dataset")
        )
        key = f"exports/{job.organization_id or 'global'}/{job.id}/{filename}"
        await get_storage().put(key, content, content_type)
        job.result_key = key
        job.result_filename = filename
        job.result_content_type = content_type
        job.result_json = json.dumps({"size_bytes": len(content)})
        await session.commit()
        await _notify(session, job.user_id, "Export ready", f"{filename} is ready to download.")

    await _run_tracked(job_id, body)


async def run_import(job_id: str) -> None:
    async def body(session: AsyncSession, job: Job) -> None:
        from app.services import import_service

        result = await import_service.commit_import(session, job)
        job.result_json = json.dumps(result)
        await session.commit()
        await _notify(
            session,
            job.user_id,
            "Import finished",
            f"Created {result.get('created', 0)} {json.loads(job.params_json).get('kind', 'records')}.",
        )

    await _run_tracked(job_id, body)


async def fanout_announcement(announcement_id: str) -> None:
    from app.models.engagement import Announcement, Notification
    from app.services.engagement_service import announcement_recipients

    async with _open_session() as session:
        announcement = await session.get(Announcement, announcement_id)
        if announcement is None:
            return
        recipients = await announcement_recipients(session, announcement)
        body = announcement.content[:500]
        for start in range(0, len(recipients), FANOUT_BATCH):
            batch = recipients[start : start + FANOUT_BATCH]
            await session.execute(
                insert(Notification),
                [
                    {
                        "id": uuid.uuid4().hex,
                        "user_id": user_id,
                        "title": f"Announcement: {announcement.title}"[:300],
                        "body": body,
                        "created_at": datetime.now(UTC),
                        "updated_at": datetime.now(UTC),
                    }
                    for user_id in batch
                ],
            )
            await session.commit()
        log.info("announcement_fanout", announcement_id=announcement_id, recipients=len(recipients))


RUNNERS: dict[str, Callable[..., Awaitable[None]]] = {
    "run_export": run_export,
    "run_import": run_import,
    "fanout_announcement": fanout_announcement,
}
