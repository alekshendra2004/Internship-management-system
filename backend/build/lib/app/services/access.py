"""Tenant isolation and object-level authorization.

Every read/write path resolves the target object through these helpers instead of
``session.get`` + role checks, so that:

* SUPER_ADMIN may act across organizations;
* ADMIN / MANAGER may only touch objects whose cohort belongs to their organization;
* INTERN may only touch their own cohort, and only their own records within it.

Objects outside the caller's scope are reported as 404 (not 403) so their existence
does not leak across tenants.
"""
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.models.cohort import Cohort, CohortStatus
from app.models.intern import Intern, InternStatus
from app.models.internship import Submission, Task, TaskAssignment
from app.models.user import Role, User

STAFF_ROLES = (Role.MANAGER, Role.ADMIN, Role.SUPER_ADMIN)
WRITABLE_STATUSES = {CohortStatus.PLANNED, CohortStatus.ACTIVE}


def not_found(entity: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{entity} not found")


def is_staff(user: User) -> bool:
    return user.role in STAFF_ROLES


async def own_intern(session: AsyncSession, user: User) -> Intern | None:
    """The intern profile linked to ``user`` (cached on the user object per request)."""
    cached = getattr(user, "_own_intern", None)
    if cached is not None:
        return cached if isinstance(cached, Intern) else None
    result = await session.execute(select(Intern).where(Intern.user_id == user.id))
    intern = result.scalar_one_or_none()
    user._own_intern = intern if intern is not None else False  # type: ignore[attr-defined]
    return intern


async def require_own_intern(session: AsyncSession, user: User) -> Intern:
    intern = await own_intern(session, user)
    if intern is None:
        raise HTTPException(status_code=403, detail="No intern profile for this account")
    return intern


def ensure_writable(cohort: Cohort) -> None:
    if cohort.status not in WRITABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Cohort is archived or completed; records are read-only",
        )


async def can_access_cohort(session: AsyncSession, user: User, cohort: Cohort) -> bool:
    if user.role == Role.SUPER_ADMIN:
        return True
    if user.role in (Role.ADMIN, Role.MANAGER):
        return user.organization_id is not None and user.organization_id == cohort.organization_id
    intern = await own_intern(session, user)
    return intern is not None and intern.cohort_id == cohort.id


async def load_cohort(
    session: AsyncSession, user: User, cohort_id: str, *, write: bool = False
) -> Cohort:
    cohort = await session.get(Cohort, cohort_id)
    if cohort is None or not await can_access_cohort(session, user, cohort):
        raise not_found("Cohort")
    if write:
        ensure_writable(cohort)
    return cohort


async def load_intern(
    session: AsyncSession, user: User, intern_id: str, *, write: bool = False
) -> tuple[Intern, Cohort]:
    intern = await session.get(Intern, intern_id)
    if intern is None:
        raise not_found("Intern")
    if user.role == Role.INTERN and intern.user_id != user.id:
        raise not_found("Intern")
    cohort = await session.get(Cohort, intern.cohort_id)
    if cohort is None or not await can_access_cohort(session, user, cohort):
        raise not_found("Intern")
    if write:
        ensure_writable(cohort)
    return intern, cohort


async def load_task(
    session: AsyncSession, user: User, task_id: str, *, write: bool = False
) -> tuple[Task, Cohort]:
    task = await session.get(Task, task_id)
    if task is None:
        raise not_found("Task")
    cohort = await session.get(Cohort, task.cohort_id)
    if cohort is None or not await can_access_cohort(session, user, cohort):
        raise not_found("Task")
    if user.role == Role.INTERN:
        intern = await own_intern(session, user)
        assigned = await session.execute(
            select(TaskAssignment.id).where(
                TaskAssignment.task_id == task.id,
                TaskAssignment.intern_id == (intern.id if intern else ""),
            )
        )
        if assigned.first() is None:
            raise not_found("Task")
    if write:
        ensure_writable(cohort)
    return task, cohort


async def load_submission(
    session: AsyncSession, user: User, submission_id: str, *, write: bool = False
) -> tuple[Submission, Task, Cohort]:
    submission = await session.get(Submission, submission_id)
    if submission is None:
        raise not_found("Submission")
    if user.role == Role.INTERN:
        intern = await own_intern(session, user)
        if intern is None or intern.id != submission.intern_id:
            raise not_found("Submission")
    try:
        task, cohort = await load_task(session, user, submission.task_id, write=write)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise not_found("Submission") from exc
        raise
    return submission, task, cohort


async def resolve_intern_for_cohort(
    session: AsyncSession,
    user: User,
    cohort: Cohort,
    intern_id: str | None,
    *,
    field: str = "intern_id",
    require_active: bool = False,
) -> Intern:
    """Resolve the intern a record is about, validating it belongs to ``cohort``.

    Interns may only act for themselves: a supplied ``intern_id`` other than their own
    is rejected. Staff must supply an intern from the same cohort.
    """
    from app.core.errors import field_error

    if user.role == Role.INTERN:
        intern = await require_own_intern(session, user)
        if intern_id and intern_id != intern.id:
            raise field_error(field, "Interns can only submit records for themselves", 403)
    else:
        if not intern_id:
            raise field_error(field, "Intern is required")
        found = await session.get(Intern, intern_id)
        if found is None:
            raise field_error(field, "Intern not found", 404)
        intern = found
    if intern.cohort_id != cohort.id:
        raise field_error(field, "Intern does not belong to this cohort", 409)
    if require_active and intern.status != InternStatus.ACTIVE:
        raise field_error(field, "Intern is deactivated", 409)
    return intern


async def validate_task_in_cohort(
    session: AsyncSession, cohort: Cohort, task_id: str | None, *, field: str = "task_id"
) -> Task | None:
    from app.core.errors import field_error

    if not task_id:
        return None
    task = await session.get(Task, task_id)
    if task is None:
        raise field_error(field, "Task not found", 404)
    if task.cohort_id != cohort.id:
        raise field_error(field, "Task does not belong to this cohort", 409)
    return task


async def scope_by_cohort(
    session: AsyncSession,
    user: User,
    stmt: Select[Any],
    cohort_col: InstrumentedAttribute[str],
    intern_col: InstrumentedAttribute[str] | None = None,
) -> Select[Any] | None:
    """Restrict ``stmt`` to rows the user may see.

    Returns ``None`` when the user can see nothing (e.g. an intern without a profile),
    so callers can short-circuit to an empty page without querying.
    """
    if user.role == Role.SUPER_ADMIN:
        return stmt
    if user.role in (Role.ADMIN, Role.MANAGER):
        if user.organization_id is None:
            return None
        return stmt.where(
            cohort_col.in_(
                select(Cohort.id).where(Cohort.organization_id == user.organization_id)
            )
        )
    intern = await own_intern(session, user)
    if intern is None:
        return None
    stmt = stmt.where(cohort_col == intern.cohort_id)
    if intern_col is not None:
        stmt = stmt.where(intern_col == intern.id)
    return stmt


def organization_filter(user: User, org_col: InstrumentedAttribute[str | None]) -> Any:
    """Filter clause for tables that carry ``organization_id`` directly."""
    if user.role == Role.SUPER_ADMIN:
        return None
    return org_col == (user.organization_id or "")
