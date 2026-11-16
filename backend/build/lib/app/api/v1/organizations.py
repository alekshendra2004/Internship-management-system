"""Organization management routes (super admin manages; others see only their own)."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles
from app.api.pagination import Page, PageParams, paginate
from app.db.session import get_session
from app.models.cohort import Organization
from app.models.user import Role, User
from app.schemas.cohort import OrganizationCreate, OrganizationPublic, OrganizationUpdate
from app.services import cohort_service

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.get("", response_model=Page[OrganizationPublic])
async def list_organizations(
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[OrganizationPublic]:
    return await paginate(
        session,
        cohort_service.organizations_query(user),
        params,
        sort_fields={"name": Organization.name, "created_at": Organization.created_at},
        default_sort="name",
        id_column=Organization.id,
        search_columns=[Organization.name],
        serialize=OrganizationPublic.model_validate,
    )


@router.post("", response_model=OrganizationPublic, status_code=201)
async def create_organization(
    payload: OrganizationCreate,
    user: User = Depends(require_roles(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> OrganizationPublic:
    org = await cohort_service.create_organization(session, payload, user)
    return OrganizationPublic.model_validate(org)


@router.patch("/{org_id}", response_model=OrganizationPublic)
async def update_organization(
    org_id: str,
    payload: OrganizationUpdate,
    user: User = Depends(require_roles(Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> OrganizationPublic:
    org = await cohort_service.get_organization(session, user, org_id)
    updated = await cohort_service.update_organization(session, org, payload)
    return OrganizationPublic.model_validate(updated)
