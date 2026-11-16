"""Read-only audit log for admins (tenant-scoped)."""
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.api.pagination import Page, PageParams, paginate
from app.db.session import get_session
from app.models.engagement import AuditLog
from app.models.user import Role, User
from app.services import audit_service

router = APIRouter(prefix="/audit-logs", tags=["audit"])


class AuditLogPublic(BaseModel):
    id: str
    user_id: str | None
    organization_id: str | None
    action: str
    entity_type: str
    entity_id: str | None
    metadata: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_row(cls, row: AuditLog) -> "AuditLogPublic":
        return cls(
            id=row.id,
            user_id=row.user_id,
            organization_id=row.organization_id,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            metadata=json.loads(row.metadata_json or "{}"),
            created_at=row.created_at,
        )


@router.get("", response_model=Page[AuditLogPublic])
async def list_audit_logs(
    action: str | None = None,
    entity_type: str | None = None,
    actor_id: str | None = None,
    params: PageParams = Depends(),
    user: User = Depends(require_roles(Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Page[AuditLogPublic]:
    return await paginate(
        session,
        audit_service.audit_query(user, action, entity_type, actor_id),
        params,
        sort_fields={"created_at": AuditLog.created_at, "action": AuditLog.action},
        default_sort="-created_at",
        id_column=AuditLog.id,
        serialize=AuditLogPublic.from_row,
    )
