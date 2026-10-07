from __future__ import annotations

import enum
import secrets
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Date, DateTime, ForeignKey, Index, Integer, String,
                        Text, TypeDecorator, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"                          # every certificate succeeded
    COMPLETED_WITH_ERRORS = "completed_with_errors"  # mix of success and failure
    FAILED = "failed"                                # nothing could be generated


class CertStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ErrorType(str, enum.Enum):
    VALIDATION = "validation"    # bad input, rejected when the job was created
    GENERATION = "generation"    # valid input, but rendering the PDF failed


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


def new_certificate_number() -> str:
    token = secrets.token_hex(5).upper()
    return f"CERT-{token[:5]}-{token[5:]}"


class UTCDateTime(TypeDecorator):
    """Always hand back timezone-aware UTC datetimes (SQLite drops tzinfo)."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_name: Mapped[str] = mapped_column(String(150))
    issuer: Mapped[str] = mapped_column(String(100))
    issue_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.QUEUED.value, index=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Certificate.row_index"
    )


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (
        UniqueConstraint("job_id", "row_index", name="uq_cert_job_row"),
        Index("ix_cert_job_status", "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    row_index: Mapped[int] = mapped_column(Integer)  # position in the submitted list
    certificate_number: Mapped[str] = mapped_column(
        String(24), unique=True, default=new_certificate_number
    )
    recipient_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recipient_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    achievement: Mapped[str | None] = mapped_column(String(60), nullable=True)
    raw_input: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # original payload, for debugging
    status: Mapped[str] = mapped_column(String(16), default=CertStatus.PENDING.value)
    error_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(300), nullable=True)  # relative to storage dir
    generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="certificates")
