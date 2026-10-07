from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.text import clean_text


class RecipientIn(BaseModel):
    """Validated per recipient (NOT at request level) so one bad row can't sink the job."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    achievement: str | None = Field(default=None, max_length=60)

    @field_validator("name", "achievement")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return None if v is None else clean_text(v)

    @field_validator("name")
    @classmethod
    def _has_letter(cls, v: str) -> str:
        if not any(ch.isalpha() for ch in v):
            raise ValueError("must contain at least one letter")
        return v


class JobCreate(BaseModel):
    """Request-level validation: only job-wide fields are strict here."""
    event_name: str = Field(min_length=1, max_length=150)
    issuer: str = Field(default="The Organizing Committee", min_length=1, max_length=100)
    issue_date: date | None = None  # defaults to today (UTC)
    # Items are validated individually later; see services/jobs.py
    recipients: list[Any] = Field(min_length=1)

    @field_validator("event_name", "issuer")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = clean_text(v)
        if not v:
            raise ValueError("must not be blank")
        return v


class Progress(BaseModel):
    total: int
    completed: int
    failed: int
    pending: int
    processing: int
    percent_done: float  # (completed + failed) / total * 100


class FailureOut(BaseModel):
    certificate_id: str
    row_index: int
    recipient_name: str | None
    recipient_email: str | None
    error_type: str | None
    error: str | None


class JobOut(BaseModel):
    job_id: str
    status: str
    event_name: str
    issuer: str
    issue_date: date
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    progress: Progress
    failures: list[FailureOut] | None = None  # first 100; full list via /certificates?status=failed
    links: dict[str, str]


class JobList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[JobOut]


class CertificateOut(BaseModel):
    certificate_id: str
    certificate_number: str
    row_index: int
    recipient_name: str | None
    recipient_email: str | None
    achievement: str | None
    status: str
    error_type: str | None
    error: str | None
    generated_at: datetime | None
    download_url: str | None


class CertificateList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CertificateOut]


class VerificationOut(BaseModel):
    valid: bool
    certificate_number: str
    recipient_name: str
    event_name: str
    issuer: str
    issue_date: date