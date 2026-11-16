"""Audit trail for sensitive actions (role changes, deactivations, exports, downloads...)."""
import json
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, request_id_var
from app.models.engagement import AuditLog
from app.models.user import Role, User

log = get_logger("audit")


async def record(
    session: AsyncSession,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    *,
    organization_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> AuditLog:
    meta = dict(metadata or {})
    request_id = request_id_var.get()
    if request_id:
        meta.setdefault("request_id", request_id)
    entry = AuditLog(
        user_id=actor.id if actor else None,
        organization_id=organization_id or (actor.organization_id if actor else None),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata_json=json.dumps(meta, default=str, sort_keys=True),
    )
    session.add(entry)
    if commit:
        await session.commit()
    else:
        await session.flush()
    log.info(
        "audit",
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor.id if actor else None,
        organization_id=entry.organization_id,
    )
    return entry


def audit_query(
    user: User,
    action: str | None = None,
    entity_type: str | None = None,
    actor_id: str | None = None,
) -> Select[Any]:
    stmt = select(AuditLog)
    if user.role != Role.SUPER_ADMIN:
        stmt = stmt.where(AuditLog.organization_id == (user.organization_id or ""))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if actor_id:
        stmt = stmt.where(AuditLog.user_id == actor_id)
    return stmt
