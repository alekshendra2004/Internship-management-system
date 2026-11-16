"""Staff account management: create managers/admins, change roles, (de)activate.

Rules:
* admins act only on users of their own organization; super admins on anyone;
* only super admins may grant or revoke SUPER_ADMIN;
* intern accounts are managed through /interns (their role cannot be changed here);
* nobody can change their own role or deactivate themselves.
Every role change and (de)activation is written to the audit log.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.api.idempotency import Idempotency, idempotent
from app.api.pagination import Page, PageParams, paginate
from app.core.errors import field_error
from app.db.session import get_session
from app.models.cohort import Organization
from app.models.user import Role, User
from app.schemas.auth import UserPublic
from app.security import hash_password
from app.services import access, audit_service
from app.services.intern_service import email_taken

router = APIRouter(prefix="/users", tags=["users"])

ADMINS = (Role.ADMIN, Role.SUPER_ADMIN)
StaffRole = Literal["MANAGER", "ADMIN", "SUPER_ADMIN"]


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=200)
    password: str = Field(min_length=8, max_length=128)
    role: StaffRole = "MANAGER"
    organization_id: str | None = Field(default=None, max_length=36)


class RoleChange(BaseModel):
    role: StaffRole


async def _load_user(session: AsyncSession, actor: User, user_id: str) -> User:
    target = await session.get(User, user_id)
    if target is None:
        raise access.not_found("User")
    if actor.role != Role.SUPER_ADMIN and target.organization_id != actor.organization_id:
        raise access.not_found("User")
    return target


def _guard(actor: User, target: User) -> None:
    if target.id == actor.id:
        raise HTTPException(status_code=409, detail="You cannot change your own account here")
    if target.role == Role.SUPER_ADMIN and actor.role != Role.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only super admins can manage super admins")


@router.get("", response_model=Page[UserPublic])
async def list_users(
    role: Role | None = None,
    params: PageParams = Depends(),
    actor: User = Depends(require_roles(*ADMINS)),
    session: AsyncSession = Depends(get_session),
) -> Page[UserPublic]:
    stmt = select(User)
    clause = access.organization_filter(actor, User.organization_id)
    if clause is not None:
        stmt = stmt.where(clause)
    if role:
        stmt = stmt.where(User.role == role)
    return await paginate(
        session,
        stmt,
        params,
        sort_fields={"full_name": User.full_name, "email": User.email, "role": User.role, "created_at": User.created_at},
        default_sort="full_name",
        id_column=User.id,
        search_columns=[User.full_name, User.email],
        serialize=UserPublic.model_validate,
    )


@router.post("", response_model=UserPublic, status_code=201)
async def create_user(
    payload: UserCreate,
    actor: User = Depends(require_roles(*ADMINS)),
    session: AsyncSession = Depends(get_session),
    idem: Idempotency = Depends(idempotent("users.create")),
):
    if (replay := await idem.replay(payload)) is not None:
        return replay
    if payload.role == "SUPER_ADMIN" and actor.role != Role.SUPER_ADMIN:
        raise field_error("role", "Only super admins can create super admins", 403)
    org_id = payload.organization_id if actor.role == Role.SUPER_ADMIN else actor.organization_id
    if actor.role != Role.SUPER_ADMIN and payload.organization_id not in (None, actor.organization_id):
        raise field_error("organization_id", "You can only add users to your organization", 403)
    if org_id and await session.get(Organization, org_id) is None:
        raise field_error("organization_id", "Organization not found", 404)
    if payload.role != "SUPER_ADMIN" and not org_id:
        raise field_error("organization_id", "Choose an organization for this user")
    if await email_taken(session, payload.email):
        raise field_error("email", "A user with this email already exists", 409)
    user = User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=Role(payload.role),
        organization_id=org_id,
    )
    session.add(user)
    await session.flush()
    await audit_service.record(
        session,
        actor,
        "USER_CREATE",
        "user",
        user.id,
        organization_id=org_id,
        metadata={"role": payload.role, "email": user.email},
        commit=False,
    )
    await session.commit()
    await session.refresh(user)
    return await idem.store(201, UserPublic.model_validate(user))


@router.patch("/{user_id}/role", response_model=UserPublic)
async def change_role(
    user_id: str,
    payload: RoleChange,
    actor: User = Depends(require_roles(*ADMINS)),
    session: AsyncSession = Depends(get_session),
) -> UserPublic:
    target = await _load_user(session, actor, user_id)
    _guard(actor, target)
    if target.role == Role.INTERN:
        raise HTTPException(status_code=409, detail="Intern accounts are managed from the interns page")
    if payload.role == "SUPER_ADMIN" and actor.role != Role.SUPER_ADMIN:
        raise field_error("role", "Only super admins can grant super admin", 403)
    old = target.role
    target.role = Role(payload.role)
    await audit_service.record(
        session,
        actor,
        "ROLE_CHANGE",
        "user",
        target.id,
        organization_id=target.organization_id,
        metadata={"from": old.value, "to": payload.role},
        commit=False,
    )
    await session.commit()
    await session.refresh(target)
    return UserPublic.model_validate(target)


async def _set_active(session: AsyncSession, actor: User, user_id: str, active: bool) -> User:
    target = await _load_user(session, actor, user_id)
    _guard(actor, target)
    if target.role == Role.INTERN:
        raise HTTPException(status_code=409, detail="Deactivate interns from the interns page")
    target.is_active = active
    await audit_service.record(
        session,
        actor,
        "USER_REACTIVATE" if active else "USER_DEACTIVATE",
        "user",
        target.id,
        organization_id=target.organization_id,
        metadata={"role": target.role.value},
        commit=False,
    )
    await session.commit()
    await session.refresh(target)
    return target


@router.post("/{user_id}/deactivate", response_model=UserPublic)
async def deactivate_user(
    user_id: str,
    actor: User = Depends(require_roles(*ADMINS)),
    session: AsyncSession = Depends(get_session),
) -> UserPublic:
    return UserPublic.model_validate(await _set_active(session, actor, user_id, False))


@router.post("/{user_id}/reactivate", response_model=UserPublic)
async def reactivate_user(
    user_id: str,
    actor: User = Depends(require_roles(*ADMINS)),
    session: AsyncSession = Depends(get_session),
) -> UserPublic:
    return UserPublic.model_validate(await _set_active(session, actor, user_id, True))
