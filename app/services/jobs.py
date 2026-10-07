"""Job creation, validation and progress queries."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Certificate, CertStatus, ErrorType, Job, JobStatus, utcnow
from app.schemas import JobCreate, Progress, RecipientIn


def _format_errors(exc: ValidationError) -> str:
    parts = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"])
        parts.append(f"{loc}: {e['msg']}" if loc else e["msg"])
    return "; ".join(parts)


def _best_effort(raw, key: str, limit: int) -> str | None:
    """Keep what we can of a rejected row so the client can find it in the failure list."""
    if isinstance(raw, dict) and isinstance(raw.get(key), (str, int, float)):
        return str(raw[key])[:limit]
    return None


_RAW_STR_LIMIT = 300
_RAW_KEY_LIMIT = 20


def _bounded_raw(raw) -> dict:
    """What we keep of the submitted row, for debugging.

    Bounded on purpose: the API accepts arbitrary JSON per row, and storing it verbatim would
    let one request write megabytes (or NaN/Infinity, which PostgreSQL's JSON type rejects).
    """
    if not isinstance(raw, dict):
        return {"value": str(raw)[:_RAW_STR_LIMIT]}
    out = {}
    for key in list(raw)[:_RAW_KEY_LIMIT]:
        val = raw[key]
        if isinstance(val, str):
            val = val[:_RAW_STR_LIMIT]
        elif isinstance(val, float) and not math.isfinite(val):
            val = str(val)  # NaN / Infinity are not valid JSON
        elif not (val is None or isinstance(val, (bool, int, float))):
            val = f"<{type(val).__name__}>"  # nested objects/lists are not kept
        out[str(key)[:50]] = val
    return out


def create_job(db: Session, payload: JobCreate) -> Job:
    """Persist the job and one Certificate row per submitted recipient.

    Invalid rows are stored as FAILED(validation) immediately; valid rows are PENDING.
    """
    job = Job(
        event_name=payload.event_name,
        issuer=payload.issuer,
        issue_date=payload.issue_date or datetime.now(timezone.utc).date(),
        total_count=len(payload.recipients),
    )
    seen_emails: set[str] = set()

    for idx, raw in enumerate(payload.recipients):
        cert = Certificate(
            row_index=idx,
            raw_input=_bounded_raw(raw),
            recipient_name=_best_effort(raw, "name", 100),
            recipient_email=_best_effort(raw, "email", 254),
        )
        try:
            r = RecipientIn.model_validate(raw)
            key = r.email.lower()
            if key in seen_emails:
                raise ValueError("duplicate email in this job")
            seen_emails.add(key)
            cert.recipient_name, cert.recipient_email = r.name, str(r.email)
            cert.achievement = r.achievement or "Participation"
            cert.status = CertStatus.PENDING.value
        except (ValidationError, ValueError) as exc:
            cert.status = CertStatus.FAILED.value
            cert.error_type = ErrorType.VALIDATION.value
            cert.error = _format_errors(exc) if isinstance(exc, ValidationError) else str(exc)
        job.certificates.append(cert)

    if all(c.status == CertStatus.FAILED.value for c in job.certificates):
        job.status = JobStatus.FAILED.value  # nothing to process
        job.finished_at = utcnow()

    db.add(job)
    db.commit()
    return job


def get_progress(db: Session, job_id: str) -> Progress:
    rows = db.execute(
        select(Certificate.status, func.count()).where(Certificate.job_id == job_id)
        .group_by(Certificate.status)
    ).all()
    counts = {s: n for s, n in rows}
    total = sum(counts.values())
    done = counts.get(CertStatus.COMPLETED.value, 0) + counts.get(CertStatus.FAILED.value, 0)
    return Progress(
        total=total,
        completed=counts.get(CertStatus.COMPLETED.value, 0),
        failed=counts.get(CertStatus.FAILED.value, 0),
        pending=counts.get(CertStatus.PENDING.value, 0),
        processing=counts.get(CertStatus.PROCESSING.value, 0),
        percent_done=round(done / total * 100, 1) if total else 100.0,
    )


def finalize_job(db: Session, job_id: str) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    p = get_progress(db, job_id)
    if p.pending or p.processing:
        return  # still work left
    if p.failed == 0:
        job.status = JobStatus.COMPLETED.value
    elif p.completed == 0:
        job.status = JobStatus.FAILED.value
    else:
        job.status = JobStatus.COMPLETED_WITH_ERRORS.value
    job.finished_at = utcnow()
    db.commit()


def requeue_failed_generations(db: Session, job_id: str) -> int:
    """Put certificates that failed *while rendering* back to PENDING and re-open the job.

    Validation failures are not retried - the input is wrong, resubmit it corrected.
    Returns the number of certificates re-queued (0 = nothing to do).
    """
    rows = db.scalars(select(Certificate).where(
        Certificate.job_id == job_id,
        Certificate.status == CertStatus.FAILED.value,
        Certificate.error_type == ErrorType.GENERATION.value)).all()
    if not rows:
        return 0
    for c in rows:
        c.status, c.error, c.error_type = CertStatus.PENDING.value, None, None
    job = db.get(Job, job_id)
    job.status = JobStatus.QUEUED.value
    job.finished_at = None
    db.commit()
    return len(rows)
