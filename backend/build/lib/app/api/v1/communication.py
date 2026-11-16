"""Communication routes: announcements, events, notifications, reviews, criteria."""
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles
from app.api.idempotency import Idempotency, idempotent
from app.api.pagination import Page, PageParams, paginate
from app.db.session import get_session
from app.models.engagement import (
    Announcement,
    Event,
    Notification,
    PeriodicReview,
    ReviewCriterion,
)
from app.models.user import Role, User
from app.schemas.engagement import (
    AnnouncementCreate,
    AnnouncementPublic,
    CriterionCreate,
    CriterionPublic,
    EventCreate,
    EventPublic,
    NotificationPublic,
    PeriodicReviewCreate,
    PeriodicReviewPublic,
    ReviewEntryPublic,
)
from app.services import engagement_service, jobs

router = APIRouter(tags=["communication"])

STAFF = (Role.MANAGER, Role.ADMIN, Role.SUPER_ADMIN)


@router.get("/announcements", response_model=Page[AnnouncementPublic])
async def list_announcements(
    cohort_id: str | None = None,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[AnnouncementPublic]:
    stmt = await engagement_service.announcements_query(session, user, cohort_id)
    return await paginate(
        session,
        stmt,
        params,
        sort_fields={"published_at": Announcement.published_at, "title": Announcement.title},
        default_sort="-published_at",
        id_column=Announcement.id,
        search_columns=[Announcement.title, Announcement.content],
        serialize=AnnouncementPublic.model_validate,
    )


@router.post("/announcements", response_model=AnnouncementPublic, status_code=201)
async def create_announcement(
    payload: AnnouncementCreate,
    user: User = Depends(require_roles(*STAFF)),
    session: AsyncSession = Depends(get_session),
    idem: Idempotency = Depends(idempotent("announcements.create")),
):
    if (replay := await idem.replay(payload)) is not None:
        return replay
    announcement = await engagement_service.create_announcement(
        session, user, **payload.model_dump()
    )
    # Notification fan-out can touch thousands of interns: run it off the request path.
    await jobs.enqueue_announcement_fanout(announcement.id)
    return await idem.store(201, AnnouncementPublic.model_validate(announcement))


@router.get("/events", response_model=Page[EventPublic])
async def list_events(
    cohort_id: str | None = None,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[EventPublic]:
    stmt = await engagement_service.events_query(session, user, cohort_id, starts_after, starts_before)
    return await paginate(
        session,
        stmt,
        params,
        sort_fields={"start_time": Event.start_time, "title": Event.title},
        default_sort="start_time",
        id_column=Event.id,
        search_columns=[Event.title, Event.location],
        serialize=EventPublic.model_validate,
    )


@router.post("/events", response_model=EventPublic, status_code=201)
async def create_event(
    payload: EventCreate,
    user: User = Depends(require_roles(*STAFF)),
    session: AsyncSession = Depends(get_session),
    idem: Idempotency = Depends(idempotent("events.create")),
):
    if (replay := await idem.replay(payload)) is not None:
        return replay
    event = await engagement_service.create_event(session, user, **payload.model_dump())
    return await idem.store(201, EventPublic.model_validate(event))


@router.get("/notifications", response_model=Page[NotificationPublic])
async def list_notifications(
    unread_only: bool = False,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[NotificationPublic]:
    return await paginate(
        session,
        engagement_service.notifications_query(user, unread_only),
        params,
        sort_fields={"created_at": Notification.created_at},
        default_sort="-created_at",
        id_column=Notification.id,
        search_columns=[Notification.title],
        serialize=NotificationPublic.model_validate,
    )


@router.post("/notifications/read-all")
async def mark_all_read(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    updated = await engagement_service.mark_all_notifications_read(session, user)
    return {"updated": updated}


@router.post("/notifications/{notification_id}/read", response_model=NotificationPublic)
async def mark_read(
    notification_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    notification = await engagement_service.mark_notification_read(
        session, user, notification_id
    )
    return NotificationPublic.model_validate(notification)


@router.get("/reviews/criteria", response_model=Page[CriterionPublic])
async def list_criteria(
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[CriterionPublic]:
    return await paginate(
        session,
        engagement_service.criteria_query(user),
        params,
        sort_fields={"name": ReviewCriterion.name},
        default_sort="name",
        id_column=ReviewCriterion.id,
        search_columns=[ReviewCriterion.name],
        serialize=CriterionPublic.model_validate,
    )


@router.post("/reviews/criteria", response_model=CriterionPublic, status_code=201)
async def create_criterion(
    payload: CriterionCreate,
    user: User = Depends(require_roles(Role.ADMIN, Role.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    criterion = await engagement_service.create_criterion(
        session, user, payload.name, payload.description, payload.organization_id
    )
    return CriterionPublic.model_validate(criterion)


async def _with_entries(session: AsyncSession, reviews: list[PeriodicReview]) -> list[PeriodicReviewPublic]:
    entries = await engagement_service.entries_for_reviews(session, [r.id for r in reviews])
    out = []
    for review in reviews:
        data = PeriodicReviewPublic.model_validate(review)
        data.entries = [ReviewEntryPublic.model_validate(e) for e in entries[review.id]]
        out.append(data)
    return out


@router.get("/reviews", response_model=Page[PeriodicReviewPublic])
async def list_reviews(
    cohort_id: str | None = None,
    intern_id: str | None = None,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[PeriodicReviewPublic]:
    stmt = await engagement_service.reviews_query(session, user, cohort_id, intern_id)
    page = await paginate(
        session,
        stmt,
        params,
        sort_fields={"created_at": PeriodicReview.created_at, "review_type": PeriodicReview.review_type},
        default_sort="-created_at",
        id_column=PeriodicReview.id,
    )
    page.items = await _with_entries(session, page.items)
    return page


@router.post("/reviews", response_model=PeriodicReviewPublic, status_code=201)
async def create_review(
    payload: PeriodicReviewCreate,
    user: User = Depends(require_roles(*STAFF)),
    session: AsyncSession = Depends(get_session),
    idem: Idempotency = Depends(idempotent("reviews.create")),
):
    if (replay := await idem.replay(payload)) is not None:
        return replay
    review = await engagement_service.create_periodic_review(
        session,
        user,
        payload.cohort_id,
        payload.intern_id,
        payload.review_type,
        payload.overall_comments,
        payload.scores,
    )
    data = (await _with_entries(session, [review]))[0]
    return await idem.store(201, data)
