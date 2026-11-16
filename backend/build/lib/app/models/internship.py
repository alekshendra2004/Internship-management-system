"""Tasks, assignments, submissions and deliverables."""
import enum
import uuid
from datetime import date, datetime

import sqlalchemy
from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mixins import TimestampMixin
from app.db.session import Base


class TaskPriority(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class TaskStatus(str, enum.Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class SubmissionStatus(str, enum.Enum):
    PENDING = "PENDING"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REVISION_REQUIRED = "REVISION_REQUIRED"


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: uuid.uuid4().hex
    )
    cohort_id: Mapped[str] = mapped_column(
        ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    priority: Mapped[TaskPriority] = mapped_column(
        nullable=False, default=TaskPriority.MEDIUM
    )
    status: Mapped[TaskStatus] = mapped_column(
        nullable=False, default=TaskStatus.NOT_STARTED
    )
    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    expected_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        sqlalchemy.Index("ix_tasks_cohort_deadline", "cohort_id", "deadline"),
        sqlalchemy.Index("ix_tasks_title_trgm", "title", postgresql_using="gin", postgresql_ops={"title": "gin_trgm_ops"}),
    )


class TaskAssignment(Base, TimestampMixin):
    __tablename__ = "task_assignments"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: uuid.uuid4().hex
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    intern_id: Mapped[str] = mapped_column(
        ForeignKey("interns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[TaskStatus] = mapped_column(
        nullable=False, default=TaskStatus.NOT_STARTED
    )

    __table_args__ = (
        sqlalchemy.UniqueConstraint("task_id", "intern_id", name="uq_assignment_task_intern"),
    )


class Submission(Base, TimestampMixin):
    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: uuid.uuid4().hex
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    intern_id: Mapped[str] = mapped_column(
        ForeignKey("interns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    work_summary: Mapped[str] = mapped_column(Text, nullable=False)
    time_spent_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    problems: Mapped[str] = mapped_column(Text, nullable=False, default="")
    additional_work: Mapped[str] = mapped_column(Text, nullable=False, default="")
    comments: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[SubmissionStatus] = mapped_column(
        nullable=False, default=SubmissionStatus.PENDING
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    feedback: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    reviewed_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        sqlalchemy.Index("ix_submissions_created_at", "created_at"),
        sqlalchemy.Index("ix_submissions_task_intern_created", "task_id", "intern_id", "created_at"),
        sqlalchemy.UniqueConstraint("task_id", "intern_id", "revision_of", name="uq_sub_chain"),
    )
