"""CSV bulk import routes: template download, preview, commit."""
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.api.v1.jobs import JobPublic
from app.core import ratelimit
from app.core.config import get_settings
from app.core.errors import field_error
from app.db.session import get_session
from app.models.job import JobStatus
from app.models.user import Role, User
from app.security import hash_password
from app.services import access, audit_service, import_service, jobs
from app.utils.storage import content_disposition, safe_filename

router = APIRouter(
    prefix="/imports",
    tags=["imports"],
    dependencies=[Depends(ratelimit.rate_limit("import", "import_rate_limit"))],
)

ROLES_BY_KIND = {"interns": (Role.ADMIN, Role.SUPER_ADMIN), "tasks": (Role.MANAGER, Role.ADMIN, Role.SUPER_ADMIN)}


class CommitRequest(BaseModel):
    skip_invalid: bool = False


def _check_role(user: User, kind: str) -> None:
    import_service.ensure_kind(kind)
    if user.role not in ROLES_BY_KIND[kind]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")


async def _limit_body(request: Request) -> None:
    limit = get_settings().import_max_mb * 1024 * 1024
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > limit + 64 * 1024:
        raise HTTPException(status_code=413, detail=f"Import files are limited to {get_settings().import_max_mb} MB")


@router.get("/{kind}/template")
async def download_template(
    kind: str,
    user: User = Depends(require_roles(Role.MANAGER, Role.ADMIN, Role.SUPER_ADMIN)),
) -> Response:
    _check_role(user, kind)
    return Response(
        content=import_service.template_csv(kind),
        media_type="text/csv",
        headers={"Content-Disposition": content_disposition(f"{kind}_import_template.csv")},
    )


@router.post("/{kind}/preview", response_model=JobPublic, status_code=201, dependencies=[Depends(_limit_body)])
async def preview_import(
    kind: str,
    cohort_id: str = Form(..., max_length=36),
    initial_password: str | None = Form(default=None, max_length=128),
    file: UploadFile = File(...),
    user: User = Depends(require_roles(Role.MANAGER, Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> JobPublic:
    _check_role(user, kind)
    try:
        cohort = await access.load_cohort(session, user, cohort_id, write=True)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise field_error("cohort_id", "Cohort not found", 404) from exc
        raise
    filename = safe_filename(file.filename or "import.csv")
    if not filename.lower().endswith(".csv"):
        raise field_error("file", "Upload a .csv file (use the template)")
    limit = get_settings().import_max_mb * 1024 * 1024
    # Bounded read: import files are small by policy (default 2 MB) and must be parsed whole.
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=413, detail=f"Import files are limited to {get_settings().import_max_mb} MB")
    password_hash = None
    if kind == "interns":
        if not initial_password or len(initial_password) < 8:
            raise field_error("initial_password", "Set an initial password of at least 8 characters")
        password_hash = hash_password(initial_password)
    job = await import_service.preview(session, user, kind, cohort, content, filename, password_hash)
    return JobPublic.from_job(job)


@router.post("/{job_id}/commit", response_model=JobPublic, status_code=202)
async def commit_import(
    job_id: str,
    payload: CommitRequest,
    user: User = Depends(require_roles(Role.MANAGER, Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> JobPublic:
    job = await jobs.get_job_for(session, user, job_id)
    if job.kind != "import":
        raise access.not_found("Import")
    params = json.loads(job.params_json)
    _check_role(user, params["kind"])
    if params.get("stage") != "preview" or job.status != JobStatus.QUEUED:
        raise HTTPException(status_code=409, detail="This import was already committed")
    report = import_service.stats_from(job)
    if report.get("error_count") and not payload.skip_invalid:
        raise HTTPException(status_code=409, detail="Fix the reported rows or choose to skip invalid rows")
    if not report.get("valid_rows"):
        raise HTTPException(status_code=409, detail="There are no valid rows to import")
    params.update(stage="commit", skip_invalid=payload.skip_invalid)
    job.params_json = json.dumps(params)
    await session.commit()
    await audit_service.record(
        session,
        user,
        "IMPORT_COMMIT",
        "import",
        job.id,
        organization_id=job.organization_id,
        metadata={"kind": params["kind"], "rows": report.get("valid_rows"), "cohort_id": params["cohort_id"]},
    )
    if report["valid_rows"] <= get_settings().import_inline_rows:
        # Small files finish within the request so the user gets the result immediately.
        await jobs.run_import(job.id)
    else:
        await jobs.enqueue("run_import", job.id)
    await session.refresh(job)
    return JobPublic.from_job(job)
