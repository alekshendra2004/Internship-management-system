"""Pydantic schemas for engagement features."""
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class IssueCreate(BaseModel):
    cohort_id: str = Field(min_length=1, max_length=36)
    # Optional for interns (defaults to their own profile); required for staff.
    intern_id: str | None = Field(default=None, max_length=36)
    category: str = Field(pattern="^(DATA|TECHNICAL|CLARIFICATION|RESOURCE|DEPENDENCY|TIME|OTHER)$")
    severity: str = Field(pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    description: str = Field(min_length=2, max_length=8000)
    task_id: str | None = None


class IssueResolve(BaseModel):
    resolution: str = Field(min_length=2, max_length=8000)
    close: bool = False


class IssuePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cohort_id: str
    intern_id: str
    task_id: str | None
    category: str
    severity: str
    description: str
    resolution: str
    status: str
    resolved_at: datetime | None
    resolved_by: str | None
    created_at: datetime


class ActivityCreate(BaseModel):
    cohort_id: str = Field(min_length=1, max_length=36)
    intern_id: str | None = Field(default=None, max_length=36)
    activity_date: date
    description: str = Field(min_length=2, max_length=8000)
    time_spent_minutes: int = Field(default=0, ge=0, le=1440)
    task_id: str | None = None


class ActivityPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cohort_id: str
    intern_id: str
    task_id: str | None
    activity_date: date
    description: str
    time_spent_minutes: int
    created_at: datetime


class AdditionalWorkCreate(BaseModel):
    cohort_id: str = Field(min_length=1, max_length=36)
    intern_id: str | None = Field(default=None, max_length=36)
    title: str = Field(min_length=2, max_length=300)
    description: str = Field(default="", max_length=8000)
    work_date: date
    hours: int = Field(default=0, ge=0, le=24)
    impact: str = Field(default="", max_length=4000)


class AdditionalWorkPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cohort_id: str
    intern_id: str
    title: str
    description: str
    work_date: date
    hours: int
    impact: str
    created_at: datetime


class AnnouncementCreate(BaseModel):
    cohort_id: str = Field(min_length=1, max_length=36)
    title: str = Field(min_length=2, max_length=300)
    content: str = Field(min_length=2, max_length=10000)
    audience: str = Field(default="ALL_INTERNS", pattern="^(ALL_INTERNS|TEAM|INDIVIDUAL)$")
    team: str | None = Field(default=None, max_length=100)
    individual_intern_id: str | None = Field(default=None, max_length=36)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _audience(self) -> "AnnouncementCreate":
        if self.audience == "TEAM" and not (self.team and self.team.strip()):
            raise ValueError("team: Team is required for a team announcement")
        if self.audience == "INDIVIDUAL" and not self.individual_intern_id:
            raise ValueError("individual_intern_id: Choose the intern this announcement is for")
        if self.audience != "TEAM":
            self.team = None
        if self.audience != "INDIVIDUAL":
            self.individual_intern_id = None
        return self


class AnnouncementPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cohort_id: str
    title: str
    content: str
    author_id: str | None
    audience: str
    team: str | None
    individual_intern_id: str | None
    published_at: datetime | None
    expires_at: datetime | None


class EventCreate(BaseModel):
    cohort_id: str = Field(min_length=1, max_length=36)
    title: str = Field(min_length=2, max_length=300)
    description: str = Field(default="", max_length=4000)
    start_time: datetime
    end_time: datetime | None = None
    location: str = Field(default="", max_length=300)
    meeting_link: str = Field(default="", max_length=500, pattern=r"^(https?://\S+)?$")
    recurrence_rule: str | None = Field(default=None, max_length=200)

    @field_validator("recurrence_rule")
    @classmethod
    def _rrule(cls, value: str | None) -> str | None:
        if not value:
            return None
        from dateutil.rrule import rrulestr

        try:
            rrulestr(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("Not a valid recurrence rule (RFC 5545 RRULE)") from exc
        return value

    @model_validator(mode="after")
    def _times(self) -> "EventCreate":
        if self.end_time and self.end_time < self.start_time:
            raise ValueError("end_time: End time cannot be before the start time")
        return self


class EventPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cohort_id: str
    title: str
    description: str
    start_time: datetime
    end_time: datetime | None
    location: str
    meeting_link: str
    recurrence_rule: str | None
    created_by: str | None


class NotificationPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    body: str
    read_at: datetime | None
    created_at: datetime


class CriterionCreate(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    description: str = Field(default="", max_length=1000)
    organization_id: str | None = Field(default=None, max_length=36)


class CriterionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    is_active: bool
    organization_id: str | None


class ReviewScore(BaseModel):
    criterion_id: str = Field(min_length=1, max_length=36)
    score: int = Field(ge=1, le=5)
    comments: str = Field(default="", max_length=2000)


class PeriodicReviewCreate(BaseModel):
    cohort_id: str = Field(min_length=1, max_length=36)
    intern_id: str = Field(min_length=1, max_length=36)
    review_type: str = Field(pattern="^(WEEKLY|MONTHLY|MID_TERM|FINAL)$")
    overall_comments: str = Field(default="", max_length=10000)
    scores: list[ReviewScore] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _unique_criteria(self) -> "PeriodicReviewCreate":
        ids = [s.criterion_id for s in self.scores]
        if len(ids) != len(set(ids)):
            raise ValueError("scores: Each criterion can only be scored once")
        return self


class ReviewEntryPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    criterion_id: str
    score: int
    comments: str


class PeriodicReviewPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cohort_id: str
    intern_id: str
    reviewer_id: str | None
    review_type: str
    overall_comments: str
    created_at: datetime
    entries: list[ReviewEntryPublic] = []


class DeliverablePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    submission_id: str | None
    task_id: str
    intern_id: str
    cohort_id: str
    filename: str
    content_type: str
    size_bytes: int
    created_at: datetime
