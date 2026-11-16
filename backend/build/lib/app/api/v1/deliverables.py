"""Deliverable upload/download routes (streamed, size/type-limited, tenant-checked)."""
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.pagination import Page, PageParams, paginate
from app.core import ratelimit
from app.core.errors import field_error
from app.db.session import get_session
from app.models.engagement import Deliverable
from app.models.internship import Submission, TaskAssignment
from app.models.user import Role, User
from app.schemas.engagement import DeliverablePublic
from app.services import access, audit_service
from app.utils import storage
from app.utils.storage import StorageError, UploadTooLarge, get_storage

router = APIRouter(prefix="/deliverables", tags=["deliverables"])

# Multipart framing overhead allowed on top of the file itself.
MULTIPART_SLACK = 64 * 1024


async def reject_oversize_body(request: Request) -> None:
    """Refuse an oversize upload from its Content-Length before the body is parsed."""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > storage.max_upload_bytes() + MULTIPART_SLACK:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum size of {storage.max_upload_bytes() // (1024 * 1024)} MB",
        )


@router.post(
    "",
    response_model=DeliverablePublic,
    status_code=201,
    dependencies=[
        Depends(reject_oversize_body),
        Depends(ratelimit.rate_limit("upload", "upload_rate_limit")),
    ],
)
async def upload_deliverable(
    task_id: str,
    submission_id: str | None = None,
    file: UploadFile | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if file is None:
        raise field_error("file", "No file provided")
    intern = await access.own_intern(session, user)
    if intern is None:
        raise HTTPException(status_code=403, detail="Only interns can upload deliverables")
    task, cohort = await access.load_task(session, user, task_id, write=True)
    assigned = await session.execute(
        select(TaskAssignment.id).where(
            TaskAssignment.task_id == task.id, TaskAssignment.intern_id == intern.id
        )
    )
    if assigned.first() is None:
        raise HTTPException(status_code=409, detail="Task is not assigned to you")
    if submission_id:
        submission = await session.get(Submission, submission_id)
        if submission is None or submission.task_id != task.id or submission.intern_id != intern.id:
            raise field_error("submission_id", "Submission does not belong to this task", 409)

    filename = storage.safe_filename(file.filename or "file")
    head = await file.read(storage.SNIFF_BYTES)
    try:
        _ext, content_type = storage.validate_upload_head(filename, head, file.size)
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except StorageError as exc:
        raise field_error("file", str(exc)) from exc
    await file.seek(0)

    key = storage.generate_key(f"deliverables/{cohort.id}", filename)
    backend = get_storage()
    try:
        size = await backend.save(
            key, file.file, content_type, max_bytes=storage.max_upload_bytes()
        )
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    record = Deliverable(
        submission_id=submission_id,
        task_id=task.id,
        intern_id=intern.id,
        cohort_id=cohort.id,
        filename=filename,
        storage_key=key,
        content_type=content_type,
        size_bytes=size,
    )
    session.add(record)
    try:
        await session.commit()
    except Exception:
        await backend.delete(key)
        raise
    await session.refresh(record)
    return DeliverablePublic.model_validate(record)


@router.get("/{deliverable_id}/download")
async def download_deliverable(
    deliverable_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    record = await session.get(Deliverable, deliverable_id)
    if record is None:
        raise access.not_found("Deliverable")
    if user.role == Role.INTERN:
        intern = await access.own_intern(session, user)
        if intern is None or intern.id != record.intern_id:
            raise access.not_found("Deliverable")
    try:
        cohort = await access.load_cohort(session, user, record.cohort_id)
    except HTTPException as exc:
        raise access.not_found("Deliverable") from exc
    try:
        response = await get_storage().download_response(
            record.storage_key, record.filename, record.content_type
        )
    except StorageError as exc:
        raise HTTPException(status_code=404, detail="File missing from storage") from exc
    await audit_service.record(
        session,
        user,
        "DELIVERABLE_DOWNLOAD",
        "deliverable",
        record.id,
        organization_id=cohort.organization_id,
        metadata={"filename": record.filename, "intern_id": record.intern_id},
    )
    return response


@router.get("", response_model=Page[DeliverablePublic])
async def list_deliverables(
    task_id: str | None = None,
    intern_id: str | None = None,
    submission_id: str | None = None,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[DeliverablePublic]:
    stmt = select(Deliverable)
    if task_id:
        stmt = stmt.where(Deliverable.task_id == task_id)
    if intern_id:
        stmt = stmt.where(Deliverable.intern_id == intern_id)
    if submission_id:
        stmt = stmt.where(Deliverable.submission_id == submission_id)
    scoped = await access.scope_by_cohort(
        session, user, stmt, Deliverable.cohort_id, Deliverable.intern_id
    )
    return await paginate(
        session,
        scoped,
        params,
        sort_fields={"created_at": Deliverable.created_at, "filename": Deliverable.filename},
        default_sort="-created_at",
        id_column=Deliverable.id,
        search_columns=[Deliverable.filename],
        serialize=DeliverablePublic.model_validate,
    )
