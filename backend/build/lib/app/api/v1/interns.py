"""Intern management routes."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles
from app.api.idempotency import Idempotency, idempotent
from app.api.pagination import Page, PageParams, paginate
from app.db.session import get_session
from app.models.intern import Intern, InternStatus
from app.models.user import Role, User
from app.schemas.workflow import BulkInternIds, InternCreate, InternPublic, InternUpdate
from app.services import access, audit_service, intern_service

router = APIRouter(prefix="/interns", tags=["interns"])

INTERN_SORTS = {
    "full_name": Intern.full_name,
    "email": Intern.email,
    "team": Intern.team,
    "status": Intern.status,
    "joining_date": Intern.joining_date,
    "created_at": Intern.created_at,
}


@router.get("", response_model=Page[InternPublic])
async def list_interns(
    cohort_id: str | None = None,
    team: str | None = None,
    status: InternStatus | None = None,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[InternPublic]:
    stmt = await intern_service.interns_query(session, user, cohort_id, team, status)
    return await paginate(
        session,
        stmt,
        params,
        sort_fields=INTERN_SORTS,
        default_sort="full_name",
        id_column=Intern.id,
        search_columns=[Intern.full_name, Intern.email, Intern.team],
        serialize=InternPublic.model_validate,
    )


@router.post("", response_model=InternPublic, status_code=201)
async def create_intern(
    payload: InternCreate,
    user: User = Depends(require_roles(Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
    idem: Idempotency = Depends(idempotent("interns.create")),
):
    if (replay := await idem.replay(payload)) is not None:
        return replay
    intern = await intern_service.create_intern(session, user, **payload.model_dump())
    return await idem.store(201, InternPublic.model_validate(intern))


@router.post("/bulk-deactivate", response_model=list[InternPublic])
async def bulk_deactivate(
    payload: BulkInternIds,
    user: User = Depends(require_roles(Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[InternPublic]:
    interns = await intern_service.bulk_deactivate(session, user, payload.intern_ids)
    await audit_service.record(
        session,
        user,
        "INTERN_BULK_DEACTIVATE",
        "intern",
        None,
        metadata={"intern_ids": [i.id for i in interns], "count": len(interns)},
    )
    return [InternPublic.model_validate(i) for i in interns]


@router.get("/{intern_id}", response_model=InternPublic)
async def get_intern(
    intern_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> InternPublic:
    intern, _cohort = await access.load_intern(session, user, intern_id)
    return InternPublic.model_validate(intern)


@router.patch("/{intern_id}", response_model=InternPublic)
async def update_intern(
    intern_id: str,
    payload: InternUpdate,
    user: User = Depends(require_roles(Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> InternPublic:
    intern, _cohort = await access.load_intern(session, user, intern_id, write=True)
    updates = payload.model_dump(exclude_unset=True)
    updated = await intern_service.update_intern(session, intern, updates)
    return InternPublic.model_validate(updated)


@router.post("/{intern_id}/deactivate", response_model=InternPublic)
async def deactivate_intern(
    intern_id: str,
    user: User = Depends(require_roles(Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> InternPublic:
    intern, cohort = await access.load_intern(session, user, intern_id, write=True)
    updated = await intern_service.deactivate_intern(session, intern)
    await audit_service.record(
        session,
        user,
        "INTERN_DEACTIVATE",
        "intern",
        intern.id,
        organization_id=cohort.organization_id,
        metadata={"user_id": intern.user_id, "cohort_id": cohort.id},
    )
    return InternPublic.model_validate(updated)
