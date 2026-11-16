"""Export routes.

``POST /exports/cohorts/{id}/jobs`` queues an export on the background worker and is
what the UI uses. The synchronous ``GET`` endpoints remain for small cohorts and API
clients; both paths are tenant-checked, rate-limited and audited.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.api.v1.jobs import JobPublic
from app.core import ratelimit
from app.db.session import get_session
from app.models.user import Role, User
from app.services import access, audit_service, export_service, jobs
from app.utils.storage import content_disposition

router = APIRouter(
    prefix="/exports",
    tags=["exports"],
    dependencies=[Depends(ratelimit.rate_limit("export", "export_rate_limit"))],
)

ADMINS = (Role.ADMIN, Role.SUPER_ADMIN)


class ExportJobRequest(BaseModel):
    format: Literal["xlsx", "csv", "pdf", "zip"]
    dataset: str | None = None


async def _sync_export(
    session: AsyncSession, user: User, cohort_id: str, fmt: str, dataset: str | None = None
) -> Response:
    cohort = await access.load_cohort(session, user, cohort_id)
    if fmt == "csv" and dataset not in export_service.SHEET_BUILDERS:
        raise HTTPException(status_code=404, detail=f"Unknown dataset '{dataset}'")
    _cohort, data = await export_service.get_cohort_export_data(session, cohort.id)
    content, filename, content_type = export_service.render(data, cohort, fmt, dataset)
    await audit_service.record(
        session,
        user,
        "EXPORT_COHORT",
        "cohort",
        cohort.id,
        organization_id=cohort.organization_id,
        metadata={"format": fmt, "dataset": dataset, "mode": "sync"},
    )
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.post("/cohorts/{cohort_id}/jobs", response_model=JobPublic, status_code=202)
async def queue_export(
    cohort_id: str,
    payload: ExportJobRequest,
    user: User = Depends(require_roles(*ADMINS)),
    session: AsyncSession = Depends(get_session),
) -> JobPublic:
    cohort = await access.load_cohort(session, user, cohort_id)
    if payload.format == "csv" and payload.dataset not in export_service.SHEET_BUILDERS:
        from app.core.errors import field_error

        raise field_error("dataset", f"Choose one of: {', '.join(export_service.SHEET_BUILDERS)}")
    job = await jobs.create_job(
        session,
        user,
        "export",
        {"cohort_id": cohort.id, "format": payload.format, "dataset": payload.dataset},
        cohort.organization_id,
    )
    await audit_service.record(
        session,
        user,
        "EXPORT_COHORT",
        "cohort",
        cohort.id,
        organization_id=cohort.organization_id,
        metadata={"format": payload.format, "dataset": payload.dataset, "mode": "job", "job_id": job.id},
    )
    await jobs.enqueue("run_export", job.id)
    await session.refresh(job)
    return JobPublic.from_job(job)


@router.get("/cohorts/{cohort_id}/xlsx")
async def export_xlsx(
    cohort_id: str,
    user: User = Depends(require_roles(*ADMINS)),
    session: AsyncSession = Depends(get_session),
):
    return await _sync_export(session, user, cohort_id, "xlsx")


@router.get("/cohorts/{cohort_id}/csv/{dataset}")
async def export_csv(
    cohort_id: str,
    dataset: str,
    user: User = Depends(require_roles(*ADMINS)),
    session: AsyncSession = Depends(get_session),
):
    return await _sync_export(session, user, cohort_id, "csv", dataset)


@router.get("/cohorts/{cohort_id}/pdf")
async def export_pdf(
    cohort_id: str,
    user: User = Depends(require_roles(*ADMINS)),
    session: AsyncSession = Depends(get_session),
):
    return await _sync_export(session, user, cohort_id, "pdf")


@router.get("/cohorts/{cohort_id}/zip")
async def export_zip(
    cohort_id: str,
    user: User = Depends(require_roles(*ADMINS)),
    session: AsyncSession = Depends(get_session),
):
    return await _sync_export(session, user, cohort_id, "zip")
