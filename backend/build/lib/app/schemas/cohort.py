"""Pydantic schemas for organizations and cohorts."""
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.cohort import CohortStatus


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=2000)
    contact_email: EmailStr


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    contact_email: EmailStr | None = None


class OrganizationPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    contact_email: EmailStr
    created_at: datetime


def check_date_range(start: date | None, end: date | None, end_field: str = "end_date") -> None:
    if start and end and end < start:
        raise ValueError(f"{end_field}: End date cannot be before the start date")


class CohortCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    year: int = Field(ge=2000, le=2100)
    description: str = Field(default="", max_length=4000)
    start_date: date | None = None
    end_date: date | None = None
    organization_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode="after")
    def _dates(self) -> "CohortCreate":
        check_date_range(self.start_date, self.end_date)
        return self


class CohortUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def _dates(self) -> "CohortUpdate":
        check_date_range(self.start_date, self.end_date)
        return self


class CohortPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    year: int
    description: str
    start_date: date | None
    end_date: date | None
    status: CohortStatus
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
