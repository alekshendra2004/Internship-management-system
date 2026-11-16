"""Engagement services: issues, activities, announcements, events, reviews, notifications."""
from datetime import UTC, date, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import field_error
from app.models.cohort import Cohort
from app.models.engagement import (
    Activity,
    AdditionalWork,
    Announcement,
    Event,
    Issue,
    IssueStatus,
    Notification,
    PeriodicReview,
    ReviewCriterion,
    ReviewEntry,
)
from app.models.intern import Intern
from app.models.user import Role, User
from app.schemas.engagement import ReviewScore
from app.services import access


async def _cohort_for_write(session: AsyncSession, user: User, cohort_id: str) -> Cohort:
    try:
        return await access.load_cohort(session, user, cohort_id, write=True)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise field_error("cohort_id", "Cohort not found", 404) from exc
        raise


# --- issues -----------------------------------------------------------------


async def create_issue(
    session: AsyncSession,
    actor: User,
    cohort_id: str,
    category: str,
    severity: str,
    description: str,
    intern_id: str | None = None,
    task_id: str | None = None,
) -> Issue:
    cohort = await _cohort_for_write(session, actor, cohort_id)
    intern = await access.resolve_intern_for_cohort(session, actor, cohort, intern_id)
    await access.validate_task_in_cohort(session, cohort, task_id)
    issue = Issue(
        cohort_id=cohort.id,
        intern_id=intern.id,
        task_id=task_id,
        category=category,
        severity=severity,
        description=description,
        status=IssueStatus.OPEN,
    )
    session.add(issue)
    await session.commit()
    await session.refresh(issue)
    return issue


async def issues_query(
    session: AsyncSession,
    actor: User,
    cohort_id: str | None = None,
    status: IssueStatus | None = None,
    severity: str | None = None,
    intern_id: str | None = None,
) -> Select[Any] | None:
    stmt = select(Issue)
    if cohort_id:
        stmt = stmt.where(Issue.cohort_id == cohort_id)
    if status:
        stmt = stmt.where(Issue.status == status)
    if severity:
        stmt = stmt.where(Issue.severity == severity)
    if intern_id:
        stmt = stmt.where(Issue.intern_id == intern_id)
    return await access.scope_by_cohort(session, actor, stmt, Issue.cohort_id, Issue.intern_id)


async def resolve_issue(
    session: AsyncSession, actor: User, issue_id: str, resolution: str, close: bool
) -> Issue:
    issue = await session.get(Issue, issue_id)
    if issue is None:
        raise access.not_found("Issue")
    try:
        await access.load_cohort(session, actor, issue.cohort_id, write=True)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise access.not_found("Issue") from exc
        raise
    issue.resolution = resolution
    issue.status = IssueStatus.CLOSED if close else IssueStatus.RESOLVED
    issue.resolved_at = datetime.now(UTC)
    issue.resolved_by = actor.id
    await session.commit()
    await session.refresh(issue)
    return issue


# --- activities & additional work -------------------------------------------


async def log_activity(
    session: AsyncSession,
    actor: User,
    cohort_id: str,
    activity_date: date,
    description: str,
    time_spent_minutes: int = 0,
    intern_id: str | None = None,
    task_id: str | None = None,
) -> Activity:
    cohort = await _cohort_for_write(session, actor, cohort_id)
    intern = await access.resolve_intern_for_cohort(session, actor, cohort, intern_id)
    await access.validate_task_in_cohort(session, cohort, task_id)
    if activity_date > datetime.now(UTC).date():
        raise field_error("activity_date", "Activity date cannot be in the future")
    entry = Activity(
        cohort_id=cohort.id,
        intern_id=intern.id,
        task_id=task_id,
        activity_date=activity_date,
        description=description,
        time_spent_minutes=time_spent_minutes,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


async def activities_query(
    session: AsyncSession,
    actor: User,
    cohort_id: str | None = None,
    intern_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Select[Any] | None:
    stmt = select(Activity)
    if cohort_id:
        stmt = stmt.where(Activity.cohort_id == cohort_id)
    if intern_id:
        stmt = stmt.where(Activity.intern_id == intern_id)
    if date_from:
        stmt = stmt.where(Activity.activity_date >= date_from)
    if date_to:
        stmt = stmt.where(Activity.activity_date <= date_to)
    return await access.scope_by_cohort(session, actor, stmt, Activity.cohort_id, Activity.intern_id)


async def create_additional_work(
    session: AsyncSession,
    actor: User,
    cohort_id: str,
    title: str,
    work_date: date,
    description: str = "",
    hours: int = 0,
    impact: str = "",
    intern_id: str | None = None,
) -> AdditionalWork:
    cohort = await _cohort_for_write(session, actor, cohort_id)
    intern = await access.resolve_intern_for_cohort(session, actor, cohort, intern_id)
    if work_date > datetime.now(UTC).date():
        raise field_error("work_date", "Work date cannot be in the future")
    record = AdditionalWork(
        cohort_id=cohort.id,
        intern_id=intern.id,
        title=title,
        description=description,
        work_date=work_date,
        hours=hours,
        impact=impact,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def additional_work_query(
    session: AsyncSession,
    actor: User,
    cohort_id: str | None = None,
    intern_id: str | None = None,
) -> Select[Any] | None:
    stmt = select(AdditionalWork)
    if cohort_id:
        stmt = stmt.where(AdditionalWork.cohort_id == cohort_id)
    if intern_id:
        stmt = stmt.where(AdditionalWork.intern_id == intern_id)
    return await access.scope_by_cohort(
        session, actor, stmt, AdditionalWork.cohort_id, AdditionalWork.intern_id
    )


# --- announcements & events ---------------------------------------------------


async def create_announcement(
    session: AsyncSession,
    actor: User,
    cohort_id: str,
    title: str,
    content: str,
    audience: str = "ALL_INTERNS",
    team: str | None = None,
    individual_intern_id: str | None = None,
    expires_at: datetime | None = None,
) -> Announcement:
    cohort = await _cohort_for_write(session, actor, cohort_id)
    if audience == "INDIVIDUAL":
        target = await session.get(Intern, individual_intern_id or "")
        if target is None or target.cohort_id != cohort.id:
            raise field_error(
                "individual_intern_id", "Intern does not belong to this cohort", 409
            )
    now = datetime.now(UTC)
    if expires_at is not None:
        expires = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        if expires <= now:
            raise field_error("expires_at", "Expiry must be in the future")
    announcement = Announcement(
        cohort_id=cohort.id,
        title=title,
        content=content,
        author_id=actor.id,
        audience=audience,
        team=team,
        individual_intern_id=individual_intern_id,
        published_at=now,
        expires_at=expires_at,
    )
    session.add(announcement)
    await session.commit()
    await session.refresh(announcement)
    return announcement


async def announcements_query(
    session: AsyncSession, actor: User, cohort_id: str | None = None
) -> Select[Any] | None:
    stmt = select(Announcement)
    if cohort_id:
        stmt = stmt.where(Announcement.cohort_id == cohort_id)
    scoped = await access.scope_by_cohort(session, actor, stmt, Announcement.cohort_id)
    if scoped is None or actor.role != Role.INTERN:
        return scoped
    intern = await access.own_intern(session, actor)
    assert intern is not None  # scope_by_cohort returns None for interns without a profile
    now = datetime.now(UTC)
    return scoped.where(
        or_(
            Announcement.audience == "ALL_INTERNS",
            and_(Announcement.audience == "TEAM", Announcement.team == intern.team),
            and_(
                Announcement.audience == "INDIVIDUAL",
                Announcement.individual_intern_id == intern.id,
            ),
        ),
        or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
    )


async def announcement_recipients(session: AsyncSession, announcement: Announcement) -> list[str]:
    """User ids an announcement should notify (active interns in the target audience)."""
    from app.models.intern import InternStatus

    stmt = select(Intern.user_id).where(
        Intern.cohort_id == announcement.cohort_id, Intern.status == InternStatus.ACTIVE
    )
    if announcement.audience == "TEAM":
        stmt = stmt.where(Intern.team == announcement.team)
    elif announcement.audience == "INDIVIDUAL":
        stmt = stmt.where(Intern.id == announcement.individual_intern_id)
    return list((await session.execute(stmt)).scalars().all())


async def create_event(
    session: AsyncSession,
    actor: User,
    cohort_id: str,
    title: str,
    start_time: datetime,
    description: str = "",
    end_time: datetime | None = None,
    location: str = "",
    meeting_link: str = "",
    recurrence_rule: str | None = None,
) -> Event:
    cohort = await _cohort_for_write(session, actor, cohort_id)
    event = Event(
        cohort_id=cohort.id,
        title=title,
        description=description,
        start_time=start_time,
        end_time=end_time,
        location=location,
        meeting_link=meeting_link,
        recurrence_rule=recurrence_rule,
        created_by=actor.id,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


def expand_event_occurrences(event: Event, window_start, window_end) -> list:
    from dateutil.rrule import rrulestr

    if not event.recurrence_rule:
        if window_start <= event.start_time <= window_end:
            return [event.start_time]
        return []
    try:
        rule = rrulestr(event.recurrence_rule, dtstart=event.start_time.replace(tzinfo=None))
    except ValueError:
        return [event.start_time] if window_start <= event.start_time <= window_end else []
    occurrences: list = []
    for occ in rule.between(
        window_start.replace(tzinfo=None), window_end.replace(tzinfo=None), inc=True
    ):
        if len(occurrences) >= 100:
            break
        occurrences.append(occ)
    return occurrences


async def events_query(
    session: AsyncSession,
    actor: User,
    cohort_id: str | None = None,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
) -> Select[Any] | None:
    stmt = select(Event)
    if cohort_id:
        stmt = stmt.where(Event.cohort_id == cohort_id)
    if starts_after:
        stmt = stmt.where(Event.start_time >= starts_after)
    if starts_before:
        stmt = stmt.where(Event.start_time <= starts_before)
    return await access.scope_by_cohort(session, actor, stmt, Event.cohort_id)


# --- notifications ------------------------------------------------------------


async def create_notification(
    session: AsyncSession, user_id: str, title: str, body: str = ""
) -> Notification:
    notification = Notification(user_id=user_id, title=title, body=body)
    session.add(notification)
    await session.commit()
    await session.refresh(notification)
    return notification


def notifications_query(user: User, unread_only: bool = False) -> Select[Any]:
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    return stmt


async def mark_notification_read(session: AsyncSession, user: User, notification_id: str) -> Notification:
    notification = await session.get(Notification, notification_id)
    if notification is None or notification.user_id != user.id:
        raise access.not_found("Notification")
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(notification)
    return notification


async def mark_all_notifications_read(session: AsyncSession, user: User) -> int:
    from sqlalchemy import update

    result = await session.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


# --- criteria & periodic reviews ----------------------------------------------


def criteria_query(user: User) -> Select[Any]:
    stmt = select(ReviewCriterion).where(ReviewCriterion.is_active.is_(True))
    if user.role != Role.SUPER_ADMIN:
        stmt = stmt.where(
            or_(
                ReviewCriterion.organization_id.is_(None),
                ReviewCriterion.organization_id == (user.organization_id or ""),
            )
        )
    return stmt


async def create_criterion(
    session: AsyncSession,
    actor: User,
    name: str,
    description: str,
    organization_id: str | None = None,
) -> ReviewCriterion:
    if actor.role not in (Role.ADMIN, Role.SUPER_ADMIN):
        raise HTTPException(status_code=403, detail="Admins only")
    org_id = organization_id if actor.role == Role.SUPER_ADMIN else actor.organization_id
    if actor.role == Role.ADMIN and organization_id and organization_id != actor.organization_id:
        raise field_error("organization_id", "You can only add criteria to your organization", 403)
    duplicate = await session.execute(
        select(ReviewCriterion.id).where(
            ReviewCriterion.name == name,
            ReviewCriterion.organization_id.is_(None)
            if org_id is None
            else ReviewCriterion.organization_id == org_id,
        )
    )
    if duplicate.first():
        raise field_error("name", "A criterion with this name already exists", 409)
    criterion = ReviewCriterion(name=name, description=description, organization_id=org_id)
    session.add(criterion)
    await session.commit()
    await session.refresh(criterion)
    return criterion


async def create_periodic_review(
    session: AsyncSession,
    actor: User,
    cohort_id: str,
    intern_id: str,
    review_type: str,
    overall_comments: str,
    scores: list[ReviewScore],
) -> PeriodicReview:
    cohort = await _cohort_for_write(session, actor, cohort_id)
    intern = await access.resolve_intern_for_cohort(session, actor, cohort, intern_id)
    for index, entry in enumerate(scores):
        criterion = await session.get(ReviewCriterion, entry.criterion_id)
        if (
            criterion is None
            or not criterion.is_active
            or criterion.organization_id not in (None, cohort.organization_id)
        ):
            raise field_error(f"scores.{index}.criterion_id", "Criterion not found", 404)
    review = PeriodicReview(
        cohort_id=cohort.id,
        intern_id=intern.id,
        reviewer_id=actor.id,
        review_type=review_type,
        overall_comments=overall_comments,
    )
    session.add(review)
    await session.flush()
    for entry in scores:
        session.add(
            ReviewEntry(
                review_id=review.id,
                criterion_id=entry.criterion_id,
                score=entry.score,
                comments=entry.comments,
            )
        )
    await session.commit()
    await session.refresh(review)
    return review


async def reviews_query(
    session: AsyncSession,
    actor: User,
    cohort_id: str | None = None,
    intern_id: str | None = None,
) -> Select[Any] | None:
    stmt = select(PeriodicReview)
    if cohort_id:
        stmt = stmt.where(PeriodicReview.cohort_id == cohort_id)
    if intern_id:
        stmt = stmt.where(PeriodicReview.intern_id == intern_id)
    return await access.scope_by_cohort(
        session, actor, stmt, PeriodicReview.cohort_id, PeriodicReview.intern_id
    )


async def entries_for_reviews(
    session: AsyncSession, review_ids: list[str]
) -> dict[str, list[ReviewEntry]]:
    out: dict[str, list[ReviewEntry]] = {rid: [] for rid in review_ids}
    if not review_ids:
        return out
    result = await session.execute(select(ReviewEntry).where(ReviewEntry.review_id.in_(review_ids)))
    for entry in result.scalars().all():
        out[entry.review_id].append(entry)
    return out
