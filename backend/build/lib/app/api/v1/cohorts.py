"""Cohort lifecycle routes."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles
from app.api.idempotency import Idempotency, idempotent
from app.api.pagination import Page, PageParams, paginate
from app.db.session import get_session
from app.models.cohort import Cohort, CohortStatus
from app.models.user import Role, User
from app.schemas.cohort import CohortCreate, CohortPublic, CohortUpdate
from app.services import access, cohort_service

router = APIRouter(prefix="/cohorts", tags=["cohorts"])

COHORT_SORTS = {
    "year": Cohort.year,
    "name": Cohort.name,
    "status": Cohort.status,
    "start_date": Cohort.start_date,
    "created_at": Cohort.created_at,
}


@router.get("", response_model=Page[CohortPublic])
async def list_cohorts(
    organization_id: str | None = None,
    status: CohortStatus | None = None,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[CohortPublic]:
    stmt = await cohort_service.cohorts_query(session, user)
    if stmt is not None and organization_id:
        stmt = stmt.where(Cohort.organization_id == organization_id)
    if stmt is not None and status:
        stmt = stmt.where(Cohort.status == status)
    return await paginate(
        session,
        stmt,
        params,
        sort_fields=COHORT_SORTS,
        default_sort="-year",
        id_column=Cohort.id,
        search_columns=[Cohort.name],
        serialize=CohortPublic.model_validate,
    )


@router.post("", response_model=CohortPublic, status_code=201)
async def create_cohort(
    payload: CohortCreate,
    user: User = Depends(require_roles(Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
    idem: Idempotency = Depends(idempotent("cohorts.create")),
):
    if (replay := await idem.replay(payload)) is not None:
        return replay
    org = await cohort_service.resolve_target_org(session, user, payload.organization_id)
    cohort = await cohort_service.create_cohort(session, payload, org)
    return await idem.store(201, CohortPublic.model_validate(cohort))


@router.get("/{cohort_id}", response_model=CohortPublic)
async def get_cohort(
    cohort_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CohortPublic:
    cohort = await access.load_cohort(session, user, cohort_id)
    return CohortPublic.model_validate(cohort)


@router.patch("/{cohort_id}", response_model=CohortPublic)
async def update_cohort(
    cohort_id: str,
    payload: CohortUpdate,
    user: User = Depends(require_roles(Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> CohortPublic:
    cohort = await access.load_cohort(session, user, cohort_id, write=True)
    updated = await cohort_service.update_cohort(session, cohort, payload)
    return CohortPublic.model_validate(updated)


@router.post("/{cohort_id}/transition", response_model=CohortPublic)
async def transition_cohort(
    cohort_id: str,
    new_status: CohortStatus,
    user: User = Depends(require_roles(Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> CohortPublic:
    cohort = await access.load_cohort(session, user, cohort_id)
    updated = await cohort_service.transition_cohort(session, cohort, new_status)
    return CohortPublic.model_validate(updated)


@router.post("/{cohort_id}/archive", response_model=CohortPublic)
async def archive_cohort(
    cohort_id: str,
    user: User = Depends(require_roles(Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> CohortPublic:
    cohort = await access.load_cohort(session, user, cohort_id)
    updated = await cohort_service.transition_cohort(
        session, cohort, CohortStatus.ARCHIVED
    )
    return CohortPublic.model_validate(updated)
