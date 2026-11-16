"""Background job status and result download."""
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.pagination import Page, PageParams, paginate
from app.db.session import get_session
from app.models.job import Job, JobStatus
from app.models.user import User
from app.services import audit_service, jobs
from app.utils.storage import StorageError, get_storage

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobPublic(BaseModel):
    id: str
    kind: str
    status: JobStatus
    params: dict[str, Any]
    result: dict[str, Any]
    error: str
    progress: int
    has_file: bool
    result_filename: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_job(cls, job: Job) -> "JobPublic":
        return cls(
            id=job.id,
            kind=job.kind,
            status=job.status,
            params=json.loads(job.params_json or "{}"),
            result=json.loads(job.result_json or "{}"),
            error=job.error,
            progress=job.progress,
            has_file=bool(job.result_key),
            result_filename=job.result_filename,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )


@router.get("", response_model=Page[JobPublic])
async def list_jobs(
    kind: str | None = None,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[JobPublic]:
    return await paginate(
        session,
        jobs.jobs_query(user, kind),
        params,
        sort_fields={"created_at": Job.created_at, "status": Job.status},
        default_sort="-created_at",
        id_column=Job.id,
        serialize=JobPublic.from_job,
    )


@router.get("/{job_id}", response_model=JobPublic)
async def get_job(
    job_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JobPublic:
    return JobPublic.from_job(await jobs.get_job_for(session, user, job_id))


@router.get("/{job_id}/download")
async def download_job_result(
    job_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    job = await jobs.get_job_for(session, user, job_id)
    if job.status != JobStatus.SUCCEEDED or not job.result_key:
        raise HTTPException(status_code=409, detail="This job has no downloadable result yet")
    try:
        response = await get_storage().download_response(
            job.result_key, job.result_filename or "export", job.result_content_type or "application/octet-stream"
        )
    except StorageError as exc:
        raise HTTPException(status_code=404, detail="Result file missing from storage") from exc
    await audit_service.record(
        session,
        user,
        "EXPORT_DOWNLOAD",
        "job",
        job.id,
        organization_id=job.organization_id,
        metadata={"filename": job.result_filename},
    )
    return response
