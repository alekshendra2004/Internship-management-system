"""Engagement routes: issues, activities, additional work."""
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles
from app.api.idempotency import Idempotency, idempotent
from app.api.pagination import Page, PageParams, paginate
from app.db.session import get_session
from app.models.engagement import Activity, AdditionalWork, Issue, IssueSeverity, IssueStatus
from app.models.user import Role, User
from app.schemas.engagement import (
    ActivityCreate,
    ActivityPublic,
    AdditionalWorkCreate,
    AdditionalWorkPublic,
    IssueCreate,
    IssuePublic,
    IssueResolve,
)
from app.services import audit_service, engagement_service

router = APIRouter(tags=["engagement"])

ISSUE_SORTS = {
    "created_at": Issue.created_at,
    "severity": Issue.severity,
    "status": Issue.status,
    "category": Issue.category,
}
ACTIVITY_SORTS = {
    "activity_date": Activity.activity_date,
    "time_spent_minutes": Activity.time_spent_minutes,
    "created_at": Activity.created_at,
}
ADDITIONAL_SORTS = {
    "work_date": AdditionalWork.work_date,
    "hours": AdditionalWork.hours,
    "title": AdditionalWork.title,
    "created_at": AdditionalWork.created_at,
}


@router.get("/issues", response_model=Page[IssuePublic])
async def list_issues(
    cohort_id: str | None = None,
    status: IssueStatus | None = None,
    severity: IssueSeverity | None = None,
    intern_id: str | None = None,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[IssuePublic]:
    stmt = await engagement_service.issues_query(
        session, user, cohort_id, status, severity.value if severity else None, intern_id
    )
    return await paginate(
        session,
        stmt,
        params,
        sort_fields=ISSUE_SORTS,
        default_sort="-created_at",
        id_column=Issue.id,
        search_columns=[Issue.description],
        serialize=IssuePublic.model_validate,
    )


@router.post("/issues", response_model=IssuePublic, status_code=201)
async def create_issue(
    payload: IssueCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    idem: Idempotency = Depends(idempotent("issues.create")),
):
    if (replay := await idem.replay(payload)) is not None:
        return replay
    issue = await engagement_service.create_issue(session, user, **payload.model_dump())
    await audit_service.record(
        session, user, "CREATE_ISSUE", "issue", issue.id, metadata={"cohort_id": issue.cohort_id}
    )
    return await idem.store(201, IssuePublic.model_validate(issue))


@router.post("/issues/{issue_id}/resolve", response_model=IssuePublic)
async def resolve_issue(
    issue_id: str,
    payload: IssueResolve,
    user: User = Depends(require_roles(Role.MANAGER, Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> IssuePublic:
    issue = await engagement_service.resolve_issue(
        session, user, issue_id, payload.resolution, payload.close
    )
    await audit_service.record(
        session, user, "RESOLVE_ISSUE", "issue", issue.id, metadata={"cohort_id": issue.cohort_id}
    )
    return IssuePublic.model_validate(issue)


@router.get("/activities", response_model=Page[ActivityPublic])
async def list_activities(
    cohort_id: str | None = None,
    intern_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[ActivityPublic]:
    stmt = await engagement_service.activities_query(
        session, user, cohort_id, intern_id, date_from, date_to
    )
    return await paginate(
        session,
        stmt,
        params,
        sort_fields=ACTIVITY_SORTS,
        default_sort="-activity_date",
        id_column=Activity.id,
        search_columns=[Activity.description],
        serialize=ActivityPublic.model_validate,
    )


@router.post("/activities", response_model=ActivityPublic, status_code=201)
async def create_activity(
    payload: ActivityCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    idem: Idempotency = Depends(idempotent("activities.create")),
):
    if (replay := await idem.replay(payload)) is not None:
        return replay
    entry = await engagement_service.log_activity(session, user, **payload.model_dump())
    return await idem.store(201, ActivityPublic.model_validate(entry))


@router.get("/additional-work", response_model=Page[AdditionalWorkPublic])
async def list_additional_work(
    cohort_id: str | None = None,
    intern_id: str | None = None,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[AdditionalWorkPublic]:
    stmt = await engagement_service.additional_work_query(session, user, cohort_id, intern_id)
    return await paginate(
        session,
        stmt,
        params,
        sort_fields=ADDITIONAL_SORTS,
        default_sort="-work_date",
        id_column=AdditionalWork.id,
        search_columns=[AdditionalWork.title, AdditionalWork.description],
        serialize=AdditionalWorkPublic.model_validate,
    )


@router.post("/additional-work", response_model=AdditionalWorkPublic, status_code=201)
async def create_additional_work(
    payload: AdditionalWorkCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    idem: Idempotency = Depends(idempotent("additional_work.create")),
):
    if (replay := await idem.replay(payload)) is not None:
        return replay
    record = await engagement_service.create_additional_work(session, user, **payload.model_dump())
    return await idem.store(201, AdditionalWorkPublic.model_validate(record))
