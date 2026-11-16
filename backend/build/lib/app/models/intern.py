"""Intern profiles and cohort membership."""
import enum
import uuid
from datetime import date

import sqlalchemy
from sqlalchemy import Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mixins import TimestampMixin
from app.db.session import Base


class InternStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DEACTIVATED = "DEACTIVATED"


class Intern(Base, TimestampMixin):
    __tablename__ = "interns"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: uuid.uuid4().hex
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    cohort_id: Mapped[str] = mapped_column(
        ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    institution: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    course: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    team: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    joining_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[InternStatus] = mapped_column(
        nullable=False, default=InternStatus.ACTIVE
    )

    __table_args__ = (
        sqlalchemy.Index("ix_interns_cohort_full_name", "cohort_id", "full_name"),
        sqlalchemy.Index("ix_interns_full_name_trgm", "full_name", postgresql_using="gin", postgresql_ops={"full_name": "gin_trgm_ops"}),
        sqlalchemy.Index("ix_interns_email_trgm", "email", postgresql_using="gin", postgresql_ops={"email": "gin_trgm_ops"}),
    )
