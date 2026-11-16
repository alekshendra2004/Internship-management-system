"""Intern management: cohort-scoped profiles linked to user accounts."""
from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import field_error
from app.models.cohort import Cohort
from app.models.intern import Intern, InternStatus
from app.models.user import Role, User
from app.security import hash_password
from app.services import access


async def interns_query(
    session: AsyncSession,
    user: User,
    cohort_id: str | None = None,
    team: str | None = None,
    status: InternStatus | None = None,
) -> Select[Any] | None:
    stmt = select(Intern)
    if cohort_id:
        stmt = stmt.where(Intern.cohort_id == cohort_id)
    if team:
        stmt = stmt.where(Intern.team == team)
    if status:
        stmt = stmt.where(Intern.status == status)
    scoped = await access.scope_by_cohort(session, user, stmt, Intern.cohort_id)
    if scoped is not None and user.role == Role.INTERN:
        scoped = scoped.where(Intern.user_id == user.id)
    return scoped


async def email_taken(session: AsyncSession, email: str) -> bool:
    result = await session.execute(select(User.id).where(func.lower(User.email) == email.lower()))
    return result.first() is not None


async def create_intern(
    session: AsyncSession,
    actor: User,
    cohort_id: str,
    full_name: str,
    email: str,
    password: str,
    phone: str = "",
    institution: str = "",
    course: str = "",
    team: str = "",
    joining_date: date | None = None,
    end_date: date | None = None,
    *,
    password_hash: str | None = None,
    commit: bool = True,
) -> Intern:
    try:
        cohort = await access.load_cohort(session, actor, cohort_id, write=True)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise field_error("cohort_id", "Cohort not found", 404) from exc
        raise
    if await email_taken(session, email):
        raise field_error("email", "A user with this email already exists", 409)
    user = User(
        email=email.lower(),
        full_name=full_name,
        password_hash=password_hash or hash_password(password),
        role=Role.INTERN,
        organization_id=cohort.organization_id,
    )
    session.add(user)
    await session.flush()
    intern = Intern(
        user_id=user.id,
        cohort_id=cohort.id,
        full_name=full_name,
        email=email.lower(),
        phone=phone,
        institution=institution,
        course=course,
        team=team,
        joining_date=joining_date,
        end_date=end_date,
        status=InternStatus.ACTIVE,
    )
    session.add(intern)
    if commit:
        await session.commit()
        await session.refresh(intern)
    else:
        await session.flush()
    return intern


async def update_intern(
    session: AsyncSession, intern: Intern, updates: dict
) -> Intern:
    joining = updates.get("joining_date") or intern.joining_date
    end = updates.get("end_date") or intern.end_date
    if joining and end and end < joining:
        raise field_error("end_date", "End date cannot be before the start date")
    for field in (
        "full_name",
        "phone",
        "institution",
        "course",
        "team",
        "joining_date",
        "end_date",
    ):
        if field in updates and updates[field] is not None:
            setattr(intern, field, updates[field])
    if updates.get("full_name"):
        user = await session.get(User, intern.user_id)
        if user is not None:
            user.full_name = updates["full_name"]
    await session.commit()
    await session.refresh(intern)
    return intern


async def deactivate_intern(session: AsyncSession, intern: Intern, *, commit: bool = True) -> Intern:
    intern.status = InternStatus.DEACTIVATED
    user = await session.get(User, intern.user_id)
    if user and user.role == Role.INTERN:
        user.is_active = False
    if commit:
        await session.commit()
        await session.refresh(intern)
    return intern


async def bulk_deactivate(
    session: AsyncSession, actor: User, intern_ids: list[str]
) -> list[Intern]:
    """Deactivate many interns atomically; any out-of-scope id fails the whole batch."""
    unique_ids = list(dict.fromkeys(intern_ids))
    interns: list[Intern] = []
    for index, intern_id in enumerate(unique_ids):
        try:
            intern, _cohort = await access.load_intern(session, actor, intern_id, write=True)
        except HTTPException as exc:
            raise field_error(f"intern_ids.{index}", str(exc.detail), exc.status_code) from exc
        interns.append(intern)
    for intern in interns:
        await deactivate_intern(session, intern, commit=False)
    await session.commit()
    return interns


async def cohort_of(session: AsyncSession, intern: Intern) -> Cohort | None:
    return await session.get(Cohort, intern.cohort_id)
