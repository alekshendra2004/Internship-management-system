"""Organization and cohort business logic with cohort write-guard."""
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import field_error
from app.models.cohort import Cohort, CohortStatus, Organization
from app.models.user import Role, User
from app.schemas.cohort import (
    CohortCreate,
    CohortUpdate,
    OrganizationCreate,
    OrganizationUpdate,
)
from app.services import access

WRITABLE_STATUSES = access.WRITABLE_STATUSES
LIFECYCLE: dict[CohortStatus, set[CohortStatus]] = {
    CohortStatus.PLANNED: {CohortStatus.ACTIVE},
    CohortStatus.ACTIVE: {CohortStatus.COMPLETED},
    CohortStatus.COMPLETED: {CohortStatus.ARCHIVED},
    CohortStatus.ARCHIVED: set(),
}


async def ensure_cohort_writable(cohort: Cohort) -> None:
    access.ensure_writable(cohort)


def organizations_query(user: User) -> Select[Any]:
    stmt = select(Organization)
    if user.role != Role.SUPER_ADMIN:
        stmt = stmt.where(Organization.id == (user.organization_id or ""))
    return stmt


async def get_organization(session: AsyncSession, user: User, org_id: str) -> Organization:
    org = await session.get(Organization, org_id)
    if org is None or (user.role != Role.SUPER_ADMIN and user.organization_id != org.id):
        raise access.not_found("Organization")
    return org


async def create_organization(
    session: AsyncSession, payload: OrganizationCreate, actor: User
) -> Organization:
    duplicate = await session.execute(
        select(Organization.id).where(func.lower(Organization.name) == payload.name.lower())
    )
    if duplicate.first():
        raise field_error("name", "An organization with this name already exists", 409)
    org = Organization(
        name=payload.name,
        description=payload.description,
        contact_email=payload.contact_email,
    )
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return org


async def update_organization(
    session: AsyncSession, org: Organization, payload: OrganizationUpdate
) -> Organization:
    if payload.name is not None:
        org.name = payload.name
    if payload.description is not None:
        org.description = payload.description
    if payload.contact_email is not None:
        org.contact_email = payload.contact_email
    await session.commit()
    await session.refresh(org)
    return org


async def cohorts_query(session: AsyncSession, user: User) -> Select[Any] | None:
    stmt = select(Cohort)
    if user.role == Role.SUPER_ADMIN:
        return stmt
    if user.role in (Role.ADMIN, Role.MANAGER):
        if user.organization_id is None:
            return None
        return stmt.where(Cohort.organization_id == user.organization_id)
    intern = await access.own_intern(session, user)
    if intern is None:
        return None
    return stmt.where(Cohort.id == intern.cohort_id)


async def resolve_target_org(
    session: AsyncSession, user: User, organization_id: str | None
) -> Organization:
    """Organization a new cohort is created in.

    Admins always create in their own organization. Super admins must name one, unless
    exactly one organization exists (single-tenant installs).
    """
    if user.role != Role.SUPER_ADMIN:
        if organization_id and organization_id != user.organization_id:
            raise field_error("organization_id", "You can only create cohorts in your organization", 403)
        if user.organization_id is None:
            raise HTTPException(status_code=400, detail="Your account is not linked to an organization")
        return await get_organization(session, user, user.organization_id)
    if organization_id:
        org = await session.get(Organization, organization_id)
        if org is None:
            raise field_error("organization_id", "Organization not found", 404)
        return org
    orgs = list((await session.execute(select(Organization).limit(2))).scalars().all())
    if len(orgs) == 1:
        return orgs[0]
    if not orgs:
        raise HTTPException(status_code=400, detail="No organization exists")
    raise field_error("organization_id", "Choose the organization for this cohort")


async def create_cohort(session: AsyncSession, payload: CohortCreate, org: Organization) -> Cohort:
    existing = await session.execute(
        select(Cohort.id).where(
            Cohort.organization_id == org.id, Cohort.year == payload.year
        )
    )
    if existing.first():
        raise field_error("year", "A cohort for this year already exists", 409)
    cohort = Cohort(
        organization_id=org.id,
        name=payload.name,
        year=payload.year,
        description=payload.description,
        start_date=payload.start_date,
        end_date=payload.end_date,
        status=CohortStatus.PLANNED,
    )
    session.add(cohort)
    await session.commit()
    await session.refresh(cohort)
    return cohort


async def update_cohort(
    session: AsyncSession, cohort: Cohort, payload: CohortUpdate
) -> Cohort:
    start = payload.start_date if payload.start_date is not None else cohort.start_date
    end = payload.end_date if payload.end_date is not None else cohort.end_date
    if start and end and end < start:
        raise field_error("end_date", "End date cannot be before the start date")
    if payload.name is not None:
        cohort.name = payload.name
    if payload.description is not None:
        cohort.description = payload.description
    if payload.start_date is not None:
        cohort.start_date = payload.start_date
    if payload.end_date is not None:
        cohort.end_date = payload.end_date
    await session.commit()
    await session.refresh(cohort)
    return cohort


async def transition_cohort(
    session: AsyncSession, cohort: Cohort, new_status: CohortStatus
) -> Cohort:
    allowed = LIFECYCLE.get(cohort.status, set())
    if new_status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot transition cohort from {cohort.status.value} to {new_status.value}",
        )
    if new_status in (CohortStatus.COMPLETED, CohortStatus.ARCHIVED):
        cohort.closed_at = datetime.now(UTC)
    cohort.status = new_status
    await session.commit()
    await session.refresh(cohort)
    return cohort
