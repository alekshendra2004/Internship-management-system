"""Task lifecycle: creation, assignment, intern-driven workflow, submissions."""
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import field_error
from app.models.cohort import Cohort
from app.models.intern import Intern, InternStatus
from app.models.internship import (
    Submission,
    SubmissionStatus,
    Task,
    TaskAssignment,
    TaskPriority,
    TaskStatus,
)
from app.models.user import Role, User
from app.services import access

ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.NOT_STARTED: {TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.IN_PROGRESS: {TaskStatus.SUBMITTED, TaskStatus.CANCELLED},
    TaskStatus.SUBMITTED: {TaskStatus.UNDER_REVIEW, TaskStatus.CANCELLED},
    TaskStatus.UNDER_REVIEW: {TaskStatus.COMPLETED, TaskStatus.REVISION_REQUIRED},
    TaskStatus.REVISION_REQUIRED: {TaskStatus.IN_PROGRESS},
    TaskStatus.COMPLETED: set(),
    TaskStatus.CANCELLED: set(),
}


def validate_transition(current: TaskStatus, new: TaskStatus) -> None:
    if new not in ALLOWED_TRANSITIONS.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot move task from {current.value} to {new.value}",
        )


async def get_assignment_or_404(
    session: AsyncSession, task_id: str, intern_id: str, *, lock: bool = False
) -> TaskAssignment:
    stmt = select(TaskAssignment).where(
        TaskAssignment.task_id == task_id, TaskAssignment.intern_id == intern_id
    )
    if lock:
        stmt = stmt.with_for_update()
    assignment = (await session.execute(stmt)).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return assignment


async def tasks_query(
    session: AsyncSession,
    user: User,
    cohort_id: str | None = None,
    status: TaskStatus | None = None,
    priority: TaskPriority | None = None,
) -> Select[Any] | None:
    stmt = select(Task)
    if cohort_id:
        stmt = stmt.where(Task.cohort_id == cohort_id)
    if status:
        stmt = stmt.where(Task.status == status)
    if priority:
        stmt = stmt.where(Task.priority == priority)
    scoped = await access.scope_by_cohort(session, user, stmt, Task.cohort_id)
    if scoped is not None and user.role == Role.INTERN:
        intern = await access.own_intern(session, user)
        scoped = scoped.where(
            Task.id.in_(
                select(TaskAssignment.task_id).where(
                    TaskAssignment.intern_id == (intern.id if intern else "")
                )
            )
        )
    return scoped


async def submissions_query(
    session: AsyncSession,
    user: User,
    task_id: str | None = None,
    intern_id: str | None = None,
    status: SubmissionStatus | None = None,
    cohort_id: str | None = None,
) -> Select[Any] | None:
    stmt = select(Submission).join(Task, Submission.task_id == Task.id)
    if task_id:
        stmt = stmt.where(Submission.task_id == task_id)
    if intern_id:
        stmt = stmt.where(Submission.intern_id == intern_id)
    if status:
        stmt = stmt.where(Submission.status == status)
    if cohort_id:
        stmt = stmt.where(Task.cohort_id == cohort_id)
    return await access.scope_by_cohort(
        session, user, stmt, Task.cohort_id, Submission.intern_id
    )


async def create_task(
    session: AsyncSession,
    actor: User,
    cohort_id: str,
    title: str,
    description: str,
    priority: TaskPriority,
    start_date=None,
    deadline=None,
    expected_hours: int = 0,
    intern_ids: list[str] | None = None,
    *,
    commit: bool = True,
) -> Task:
    try:
        cohort = await access.load_cohort(session, actor, cohort_id, write=True)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise field_error("cohort_id", "Cohort not found", 404) from exc
        raise
    task = Task(
        cohort_id=cohort.id,
        title=title,
        description=description,
        priority=priority,
        status=TaskStatus.NOT_STARTED,
        created_by=actor.id,
        start_date=start_date,
        deadline=deadline,
        expected_hours=expected_hours,
    )
    session.add(task)
    await session.flush()
    if intern_ids:
        await _add_assignments(session, task, cohort, intern_ids, field="intern_ids")
    if commit:
        await session.commit()
        await session.refresh(task)
    return task


async def update_task(session: AsyncSession, task: Task, updates: dict) -> Task:
    start = updates.get("start_date") or task.start_date
    deadline = updates.get("deadline") or task.deadline
    if start and deadline and deadline < start:
        raise field_error("deadline", "End date cannot be before the start date")
    for field in ("title", "description", "priority", "start_date", "deadline", "expected_hours"):
        if updates.get(field) is not None:
            setattr(task, field, updates[field])
    await session.commit()
    await session.refresh(task)
    return task


async def _add_assignments(
    session: AsyncSession,
    task: Task,
    cohort: Cohort,
    intern_ids: list[str],
    *,
    field: str,
) -> list[TaskAssignment]:
    created: list[TaskAssignment] = []
    for index, intern_id in enumerate(dict.fromkeys(intern_ids)):
        intern = await session.get(Intern, intern_id)
        if intern is None:
            raise field_error(f"{field}.{index}", f"Intern {intern_id} not found", 404)
        if intern.cohort_id != cohort.id:
            raise field_error(f"{field}.{index}", "Intern belongs to a different cohort", 409)
        if intern.status != InternStatus.ACTIVE:
            raise field_error(f"{field}.{index}", f"{intern.full_name} is deactivated", 409)
        existing = await session.execute(
            select(TaskAssignment.id).where(
                TaskAssignment.task_id == task.id,
                TaskAssignment.intern_id == intern_id,
            )
        )
        if existing.first() is None:
            assignment = TaskAssignment(task_id=task.id, intern_id=intern_id)
            session.add(assignment)
            created.append(assignment)
    await session.flush()
    return created


async def assign_interns(
    session: AsyncSession, actor: User, task: Task, cohort: Cohort, intern_ids: list[str]
) -> list[TaskAssignment]:
    created = await _add_assignments(session, task, cohort, intern_ids, field="intern_ids")
    await session.commit()
    return created


async def assignments_for_task(session: AsyncSession, task_id: str) -> list[TaskAssignment]:
    result = await session.execute(
        select(TaskAssignment).where(TaskAssignment.task_id == task_id).order_by(TaskAssignment.created_at)
    )
    return list(result.scalars().all())


async def update_assignment_status(
    session: AsyncSession, actor: User, task_id: str, intern_id: str, new_status: TaskStatus
) -> TaskAssignment:
    task, _cohort = await access.load_task(session, actor, task_id, write=True)
    assignment = await get_assignment_or_404(session, task.id, intern_id, lock=True)
    intern = await session.get(Intern, assignment.intern_id)
    if actor.role == Role.INTERN and (intern is None or intern.user_id != actor.id):
        raise HTTPException(status_code=403, detail="Not your assignment")
    validate_transition(assignment.status, new_status)
    assignment.status = new_status
    task.status = new_status
    await session.commit()
    await session.refresh(assignment)
    return assignment


async def create_submission(
    session: AsyncSession,
    actor: User,
    task_id: str,
    intern_id: str,
    work_summary: str,
    time_spent_minutes: int,
    problems: str = "",
    additional_work: str = "",
    comments: str = "",
) -> Submission:
    intern = await session.get(Intern, intern_id)
    if intern is None:
        raise HTTPException(status_code=404, detail="Intern not found")
    if actor.role == Role.INTERN and intern.user_id != actor.id:
        raise HTTPException(status_code=403, detail="Not your task")
    task, _cohort = await access.load_task(session, actor, task_id, write=True)
    # Lock the assignment row so two concurrent submits cannot both pass the
    # "no pending submission" check below.
    assignment = await get_assignment_or_404(session, task.id, intern_id, lock=True)
    validate_transition(assignment.status, TaskStatus.SUBMITTED)
    previous = await session.execute(
        select(Submission)
        .where(
            Submission.task_id == task_id,
            Submission.intern_id == intern_id,
        )
        .order_by(Submission.created_at.desc(), Submission.id.desc())
        .limit(1)
    )
    latest = previous.scalars().first()
    if latest and latest.status == SubmissionStatus.PENDING:
        raise HTTPException(
            status_code=409, detail="A pending submission already awaits review"
        )
    submission = Submission(
        task_id=task_id,
        intern_id=intern_id,
        work_summary=work_summary,
        time_spent_minutes=time_spent_minutes,
        problems=problems,
        additional_work=additional_work,
        comments=comments,
        status=SubmissionStatus.PENDING,
        submitted_at=datetime.now(UTC),
        revision_of=latest.id if latest else None,
    )
    session.add(submission)
    assignment.status = TaskStatus.SUBMITTED
    task.status = TaskStatus.SUBMITTED
    await session.commit()
    await session.refresh(submission)
    return submission


async def review_submission(
    session: AsyncSession,
    actor: User,
    submission_id: str,
    approve: bool,
    feedback: str,
) -> Submission:
    # Object-level check: the submission's task must sit in a cohort of the reviewer's
    # organization, not merely exist.
    submission, task, _cohort = await access.load_submission(
        session, actor, submission_id, write=True
    )
    locked = await session.execute(
        select(Submission).where(Submission.id == submission.id).with_for_update()
    )
    submission = locked.scalar_one()
    if submission.status != SubmissionStatus.PENDING:
        raise HTTPException(status_code=409, detail="Submission already reviewed")
    assignment = await get_assignment_or_404(
        session, submission.task_id, submission.intern_id, lock=True
    )
    if approve:
        submission.status = SubmissionStatus.APPROVED
        assignment.status = TaskStatus.COMPLETED
        task.status = TaskStatus.COMPLETED
    else:
        submission.status = SubmissionStatus.REVISION_REQUIRED
        assignment.status = TaskStatus.REVISION_REQUIRED
        task.status = TaskStatus.REVISION_REQUIRED
    submission.feedback = feedback
    submission.reviewed_by = actor.id
    submission.reviewed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(submission)
    return submission
