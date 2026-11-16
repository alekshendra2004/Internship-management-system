"""Dashboard aggregates per role: tenant-scoped counts, cached briefly in Redis."""
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core import cache
from app.core.config import get_settings
from app.db.session import get_session
from app.models.cohort import Cohort, CohortStatus
from app.models.engagement import Issue, IssueStatus
from app.models.intern import Intern, InternStatus
from app.models.internship import (
    Submission,
    SubmissionStatus,
    Task,
    TaskAssignment,
    TaskStatus,
)
from app.models.user import Role, User
from app.services import access

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

OPEN_TASK = (TaskStatus.COMPLETED, TaskStatus.CANCELLED)


async def _count(session: AsyncSession, stmt: Select[Any] | None) -> int:
    if stmt is None:
        return 0
    return int((await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())


async def compute_stats(session: AsyncSession, user: User, cohort_id: str | None) -> dict[str, Any]:
    today = datetime.now(UTC).date()
    soon = today + timedelta(days=7)
    scope = access.scope_by_cohort

    cohorts = select(Cohort.id).where(Cohort.status == CohortStatus.ACTIVE)
    if user.role != Role.SUPER_ADMIN:
        if user.role == Role.INTERN:
            own = await access.own_intern(session, user)
            cohorts = cohorts.where(Cohort.id == (own.cohort_id if own else ""))
        else:
            cohorts = cohorts.where(Cohort.organization_id == (user.organization_id or ""))

    def by_cohort(stmt: Select[Any], col: Any) -> Select[Any]:
        return stmt.where(col == cohort_id) if cohort_id else stmt

    interns = by_cohort(select(Intern.id).where(Intern.status == InternStatus.ACTIVE), Intern.cohort_id)
    tasks = by_cohort(select(Task.id), Task.cohort_id)
    open_tasks = tasks.where(Task.status.notin_(OPEN_TASK), Task.deadline.isnot(None))
    pending = by_cohort(
        select(Submission.id)
        .join(Task, Submission.task_id == Task.id)
        .where(Submission.status == SubmissionStatus.PENDING),
        Task.cohort_id,
    )
    issues = by_cohort(
        select(Issue.id).where(Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS])),
        Issue.cohort_id,
    )

    stats: dict[str, Any] = {
        "active_cohorts": await _count(session, cohorts),
        "active_interns": await _count(session, await scope(session, user, interns, Intern.cohort_id)),
        "total_tasks": await _count(session, await scope(session, user, tasks, Task.cohort_id)),
        "overdue_tasks": await _count(
            session, await scope(session, user, open_tasks.where(Task.deadline < today), Task.cohort_id)
        ),
        "due_soon_tasks": await _count(
            session,
            await scope(
                session,
                user,
                open_tasks.where(Task.deadline >= today, Task.deadline <= soon),
                Task.cohort_id,
            ),
        ),
        "pending_reviews": await _count(
            session, await scope(session, user, pending, Task.cohort_id, Submission.intern_id)
        ),
        "open_issues": await _count(
            session, await scope(session, user, issues, Issue.cohort_id, Issue.intern_id)
        ),
    }

    if user.role == Role.INTERN:
        own = await access.own_intern(session, user)
        if own:
            stats["my_tasks"] = await _count(
                session, select(TaskAssignment.id).where(TaskAssignment.intern_id == own.id)
            )
            stats["my_pending_submissions"] = await _count(
                session,
                select(Submission.id).where(
                    Submission.intern_id == own.id,
                    Submission.status == SubmissionStatus.PENDING,
                ),
            )
    return stats


@router.get("")
async def dashboard(
    response: Response,
    cohort_id: str | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if cohort_id:
        await access.load_cohort(session, user, cohort_id)
    # Per-user key: interns see personal counts, and scoping differs per role/org.
    key = await cache.versioned_key(
        f"dashboard:{user.organization_id or 'global'}",
        user.role.value,
        user.id,
        cohort_id or "all",
    )
    ttl = get_settings().dashboard_cache_seconds
    stats, hit = await cache.cached(key, ttl, lambda: compute_stats(session, user, cohort_id))
    response.headers["X-Cache"] = "HIT" if hit else "MISS"
    response.headers["Cache-Control"] = "private, max-age=0"
    return stats
