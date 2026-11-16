"""Pydantic schemas for interns, tasks, submissions and reviews."""
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.intern import InternStatus
from app.models.internship import (
    SubmissionStatus,
    TaskPriority,
    TaskStatus,
)
from app.schemas.cohort import check_date_range

MAX_BULK_IDS = 500


class InternCreate(BaseModel):
    cohort_id: str = Field(min_length=1, max_length=36)
    full_name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    phone: str = Field(default="", max_length=40, pattern=r"^[0-9+()\-. ]*$")
    institution: str = Field(default="", max_length=200)
    course: str = Field(default="", max_length=200)
    team: str = Field(default="", max_length=100)
    joining_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def _dates(self) -> "InternCreate":
        check_date_range(self.joining_date, self.end_date)
        return self


class InternUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=40, pattern=r"^[0-9+()\-. ]*$")
    institution: str | None = Field(default=None, max_length=200)
    course: str | None = Field(default=None, max_length=200)
    team: str | None = Field(default=None, max_length=100)
    joining_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def _dates(self) -> "InternUpdate":
        check_date_range(self.joining_date, self.end_date)
        return self


class InternPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    cohort_id: str
    full_name: str
    email: EmailStr
    phone: str
    institution: str
    course: str
    team: str
    joining_date: date | None
    end_date: date | None
    status: InternStatus


class BulkInternIds(BaseModel):
    intern_ids: list[str] = Field(min_length=1, max_length=MAX_BULK_IDS)


class TaskCreate(BaseModel):
    cohort_id: str = Field(min_length=1, max_length=36)
    title: str = Field(min_length=2, max_length=300)
    description: str = Field(default="", max_length=8000)
    priority: TaskPriority = TaskPriority.MEDIUM
    start_date: date | None = None
    deadline: date | None = None
    expected_hours: int = Field(default=0, ge=0, le=1000)
    intern_ids: list[str] = Field(default_factory=list, max_length=MAX_BULK_IDS)

    @model_validator(mode="after")
    def _dates(self) -> "TaskCreate":
        check_date_range(self.start_date, self.deadline, "deadline")
        return self


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=300)
    description: str | None = Field(default=None, max_length=8000)
    priority: TaskPriority | None = None
    start_date: date | None = None
    deadline: date | None = None
    expected_hours: int | None = Field(default=None, ge=0, le=1000)

    @model_validator(mode="after")
    def _dates(self) -> "TaskUpdate":
        check_date_range(self.start_date, self.deadline, "deadline")
        return self


class TaskPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cohort_id: str
    title: str
    description: str
    priority: TaskPriority
    status: TaskStatus
    created_by: str | None
    start_date: date | None
    deadline: date | None
    expected_hours: int


class AssignmentPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    intern_id: str
    status: TaskStatus


class SubmissionCreate(BaseModel):
    work_summary: str = Field(min_length=2, max_length=8000)
    time_spent_minutes: int = Field(ge=0, le=100000)
    problems: str = Field(default="", max_length=8000)
    additional_work: str = Field(default="", max_length=8000)
    comments: str = Field(default="", max_length=8000)


class SubmissionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    intern_id: str
    work_summary: str
    time_spent_minutes: int
    problems: str
    additional_work: str
    comments: str
    status: SubmissionStatus
    submitted_at: datetime | None
    revision_of: str | None
    feedback: str = ""
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


class ReviewRequest(BaseModel):
    approve: bool
    feedback: str = Field(default="", max_length=8000)


class StatusUpdateRequest(BaseModel):
    status: TaskStatus
