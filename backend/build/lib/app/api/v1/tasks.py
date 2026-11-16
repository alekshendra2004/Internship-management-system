"""Task, assignment and submission routes."""

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles
from app.api.idempotency import Idempotency, idempotent
from app.api.pagination import Page, PageParams, paginate
from app.db.session import get_session
from app.models.internship import (
    Submission,
    SubmissionStatus,
    Task,
    TaskPriority,
    TaskStatus,
)
from app.models.user import Role, User
from app.schemas.workflow import (
    MAX_BULK_IDS,
    AssignmentPublic,
    ReviewRequest,
    StatusUpdateRequest,
    SubmissionCreate,
    SubmissionPublic,
    TaskCreate,
    TaskPublic,
    TaskUpdate,
)
from app.services import access, task_service

router = APIRouter(prefix="/tasks", tags=["tasks"])

STAFF = (Role.ADMIN, Role.MANAGER, Role.SUPER_ADMIN)

TASK_SORTS = {
    "deadline": Task.deadline,
    "title": Task.title,
    "priority": Task.priority,
    "status": Task.status,
    "created_at": Task.created_at,
}
SUBMISSION_SORTS = {
    "created_at": Submission.created_at,
    "submitted_at": Submission.submitted_at,
    "status": Submission.status,
}


@router.get("", response_model=Page[TaskPublic])
async def list_tasks(
    cohort_id: str | None = None,
    status: TaskStatus | None = None,
    priority: TaskPriority | None = None,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[TaskPublic]:
    stmt = await task_service.tasks_query(session, user, cohort_id, status, priority)
    return await paginate(
        session,
        stmt,
        params,
        sort_fields=TASK_SORTS,
        default_sort="deadline",
        id_column=Task.id,
        search_columns=[Task.title],
        serialize=TaskPublic.model_validate,
    )


@router.post("", response_model=TaskPublic, status_code=201)
async def create_task(
    payload: TaskCreate,
    user: User = Depends(require_roles(*STAFF)),
    session: AsyncSession = Depends(get_session),
    idem: Idempotency = Depends(idempotent("tasks.create")),
):
    if (replay := await idem.replay(payload)) is not None:
        return replay
    task = await task_service.create_task(session, user, **payload.model_dump())
    return await idem.store(201, TaskPublic.model_validate(task))


# Static paths are declared before "/{task_id}" so they are never captured by it.
@router.get("/submissions", response_model=Page[SubmissionPublic])
async def list_submissions(
    task_id: str | None = None,
    intern_id: str | None = None,
    cohort_id: str | None = None,
    status: SubmissionStatus | None = None,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[SubmissionPublic]:
    stmt = await task_service.submissions_query(session, user, task_id, intern_id, status, cohort_id)
    return await paginate(
        session,
        stmt,
        params,
        sort_fields=SUBMISSION_SORTS,
        default_sort="-created_at",
        id_column=Submission.id,
        serialize=SubmissionPublic.model_validate,
    )


@router.get("/submissions/{submission_id}", response_model=SubmissionPublic)
async def get_submission(
    submission_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SubmissionPublic:
    submission, _task, _cohort = await access.load_submission(session, user, submission_id)
    return SubmissionPublic.model_validate(submission)


@router.post("/submissions/{submission_id}/review", response_model=SubmissionPublic)
async def review_submission(
    submission_id: str,
    payload: ReviewRequest,
    user: User = Depends(require_roles(Role.MANAGER, Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> SubmissionPublic:
    submission = await task_service.review_submission(
        session, user, submission_id, payload.approve, payload.feedback
    )
    return SubmissionPublic.model_validate(submission)


@router.get("/{task_id}", response_model=TaskPublic)
async def get_task(
    task_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskPublic:
    task, _cohort = await access.load_task(session, user, task_id)
    return TaskPublic.model_validate(task)


@router.patch("/{task_id}", response_model=TaskPublic)
async def update_task(
    task_id: str,
    payload: TaskUpdate,
    user: User = Depends(require_roles(*STAFF)),
    session: AsyncSession = Depends(get_session),
) -> TaskPublic:
    task, _cohort = await access.load_task(session, user, task_id, write=True)
    updated = await task_service.update_task(session, task, payload.model_dump(exclude_unset=True))
    return TaskPublic.model_validate(updated)


@router.get("/{task_id}/assignments", response_model=list[AssignmentPublic])
async def list_assignments(
    task_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AssignmentPublic]:
    task, _cohort = await access.load_task(session, user, task_id)
    assignments = await task_service.assignments_for_task(session, task.id)
    if user.role == Role.INTERN:
        intern = await access.own_intern(session, user)
        assignments = [a for a in assignments if intern and a.intern_id == intern.id]
    return [AssignmentPublic.model_validate(a) for a in assignments]


@router.post("/{task_id}/assign", response_model=list[AssignmentPublic])
async def assign_task(
    task_id: str,
    intern_ids: list[str] = Body(..., max_length=MAX_BULK_IDS),
    user: User = Depends(require_roles(*STAFF)),
    session: AsyncSession = Depends(get_session),
) -> list[AssignmentPublic]:
    task, cohort = await access.load_task(session, user, task_id, write=True)
    assignments = await task_service.assign_interns(session, user, task, cohort, intern_ids)
    return [AssignmentPublic.model_validate(a) for a in assignments]


@router.post("/{task_id}/status", response_model=AssignmentPublic)
async def update_status(
    task_id: str,
    payload: StatusUpdateRequest,
    user: User = Depends(require_roles(Role.INTERN, Role.MANAGER, Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> AssignmentPublic:
    intern = await access.own_intern(session, user)
    if intern is None:
        raise HTTPException(status_code=403, detail="Only interns can update own status")
    assignment = await task_service.update_assignment_status(
        session, user, task_id, intern.id, payload.status
    )
    return AssignmentPublic.model_validate(assignment)


@router.post("/{task_id}/submit", response_model=SubmissionPublic, status_code=201)
async def submit_task(
    task_id: str,
    payload: SubmissionCreate,
    user: User = Depends(require_roles(Role.INTERN)),
    session: AsyncSession = Depends(get_session),
    idem: Idempotency = Depends(idempotent("tasks.submit")),
):
    if (replay := await idem.replay({"task_id": task_id, **payload.model_dump()})) is not None:
        return replay
    intern = await access.require_own_intern(session, user)
    submission = await task_service.create_submission(
        session, user, task_id, intern.id, **payload.model_dump()
    )
    return await idem.store(201, SubmissionPublic.model_validate(submission))
