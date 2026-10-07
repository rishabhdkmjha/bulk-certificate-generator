from __future__ import annotations

import logging
import re
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deps import get_db, require_api_key
from app.models import Certificate, CertStatus, Job, JobStatus
from app.schemas import (CertificateList, CertificateOut, FailureOut, JobCreate, JobList,
                         JobOut, VerificationOut)
from app.services import jobs as job_service
from app.services.generator import CertificateGenerationError

log = logging.getLogger("certgen")

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)], tags=["certificates"])
public_router = APIRouter(prefix="/api/v1", tags=["verification"])

MAX_FAILURES_IN_JOB_STATUS = 100


# ---------- helpers ----------------------------------------------------------
def _job_links(job_id: str) -> dict[str, str]:
    base = f"/api/v1/jobs/{job_id}"
    return {"self": base, "certificates": f"{base}/certificates", "download_all": f"{base}/download"}


def _cert_out(c: Certificate) -> CertificateOut:
    done = c.status == CertStatus.COMPLETED.value
    return CertificateOut(
        certificate_id=c.id, certificate_number=c.certificate_number, row_index=c.row_index,
        recipient_name=c.recipient_name, recipient_email=c.recipient_email,
        achievement=c.achievement, status=c.status, error_type=c.error_type, error=c.error,
        generated_at=c.generated_at,
        download_url=f"/api/v1/certificates/{c.id}/download" if done else None,
    )


def _job_out(db: Session, job: Job, include_failures: bool = False) -> JobOut:
    failures = None
    if include_failures:
        rows = db.scalars(
            select(Certificate)
            .where(Certificate.job_id == job.id, Certificate.status == CertStatus.FAILED.value)
            .order_by(Certificate.row_index).limit(MAX_FAILURES_IN_JOB_STATUS)
        ).all()
        failures = [FailureOut(certificate_id=c.id, row_index=c.row_index,
                               recipient_name=c.recipient_name, recipient_email=c.recipient_email,
                               error_type=c.error_type, error=c.error) for c in rows]
    return JobOut(
        job_id=job.id, status=job.status, event_name=job.event_name, issuer=job.issuer,
        issue_date=job.issue_date, created_at=job.created_at, started_at=job.started_at,
        finished_at=job.finished_at, progress=job_service.get_progress(db, job.id),
        failures=failures, links=_job_links(job.id),
    )


def _get_job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def _safe_file(request: Request, cert: Certificate) -> Path:
    if cert.status != CertStatus.COMPLETED.value or not cert.file_path:
        raise HTTPException(409, f"Certificate is not available (status: {cert.status})")
    root = Path(request.app.state.settings.storage_dir).resolve()
    path = (root / cert.file_path).resolve()
    if root not in path.parents or not path.is_file():  # path-traversal + missing-file guard
        raise HTTPException(404, "Certificate file not found on disk")
    return path


def _filename(cert: Certificate) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", cert.recipient_name or "").strip("-")[:40] or "recipient"
    return f"{cert.certificate_number}_{slug}.pdf"


def _preflight_job_fields(request: Request, payload: JobCreate) -> None:
    """Reject job-wide text that can never be printed (too long, unsupported characters).

    Without this, every certificate in the job would fail separately with the same error.
    """
    check = getattr(request.app.state.generator, "validate_job_fields", None)
    if check is None:  # e.g. a test double without a template
        return
    try:
        check(payload.event_name, payload.issuer)
    except CertificateGenerationError as exc:
        field = "issuer" if str(exc).startswith("issuer") else "event_name"
        raise HTTPException(422, detail=[{"loc": ["body", field], "msg": str(exc),
                                          "type": "value_error.unrenderable"}])


# ---------- jobs -------------------------------------------------------------
@router.post("/jobs", response_model=JobOut, status_code=202)
def create_job(payload: JobCreate, request: Request, response: Response,
               db: Session = Depends(get_db)):
    """Submit a bulk request. Returns immediately (202); poll the job for progress."""
    limit = request.app.state.settings.max_recipients_per_job
    if len(payload.recipients) > limit:
        raise HTTPException(413, f"Too many recipients: {len(payload.recipients)} (max {limit} per job)")
    _preflight_job_fields(request, payload)
    job = job_service.create_job(db, payload)
    if job.status == JobStatus.QUEUED.value:
        request.app.state.dispatcher.submit(job.id)
        db.refresh(job)
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    return _job_out(db, job, include_failures=True)


@router.get("/jobs", response_model=JobList)
def list_jobs(status: JobStatus | None = None, limit: int = Query(20, ge=1, le=100),
              offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    q = select(Job)
    if status:
        q = q.where(Job.status == status.value)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    jobs = db.scalars(q.order_by(Job.created_at.desc()).limit(limit).offset(offset)).all()
    return JobList(total=total, limit=limit, offset=offset, items=[_job_out(db, j) for j in jobs])


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    return _job_out(db, _get_job_or_404(db, job_id), include_failures=True)


@router.get("/jobs/{job_id}/certificates", response_model=CertificateList)
def list_certificates(job_id: str, status: CertStatus | None = None,
                      limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                      db: Session = Depends(get_db)):
    _get_job_or_404(db, job_id)
    q = select(Certificate).where(Certificate.job_id == job_id)
    if status:
        q = q.where(Certificate.status == status.value)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(Certificate.row_index).limit(limit).offset(offset)).all()
    return CertificateList(total=total, limit=limit, offset=offset, items=[_cert_out(c) for c in rows])


@router.post("/jobs/{job_id}/retry", response_model=JobOut, status_code=202)
def retry_failed(job_id: str, request: Request, db: Session = Depends(get_db)):
    """Re-generate certificates that failed while rendering (not validation failures)."""
    job = _get_job_or_404(db, job_id)
    if job.status in (JobStatus.QUEUED.value, JobStatus.PROCESSING.value):
        raise HTTPException(409, f"Job is still {job.status}; retry after it finishes")
    if job_service.requeue_failed_generations(db, job_id) == 0:
        raise HTTPException(409, "Nothing to retry: no certificate failed during generation")
    request.app.state.dispatcher.submit(job_id)
    db.refresh(job)
    return _job_out(db, job, include_failures=True)


@router.get("/jobs/{job_id}/download")
def download_job_zip(job_id: str, request: Request, db: Session = Depends(get_db)):
    """ZIP of every certificate generated so far (works while the job is still running)."""
    _get_job_or_404(db, job_id)
    certs = db.scalars(select(Certificate).where(
        Certificate.job_id == job_id, Certificate.status == CertStatus.COMPLETED.value
    ).order_by(Certificate.row_index)).all()
    if not certs:
        raise HTTPException(409, "No certificates have been generated for this job yet")

    # Build on disk, not in RAM, so a big job can't balloon memory; delete after sending.
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    added = 0
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
            for c in certs:
                try:
                    zf.write(_safe_file(request, c), arcname=_filename(c))
                    added += 1
                except HTTPException:  # one missing file must not break the whole download
                    log.warning("job %s: file for certificate %s is missing on disk", job_id, c.id)
        if added == 0:
            raise HTTPException(404, "Certificate files not found on disk")
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise
    return FileResponse(tmp.name, media_type="application/zip", filename=f"job-{job_id}.zip",
                        background=BackgroundTask(Path(tmp.name).unlink, missing_ok=True))


# ---------- certificates -----------------------------------------------------
@router.get("/certificates/{certificate_id}/download")
def download_certificate(certificate_id: str, request: Request, db: Session = Depends(get_db)):
    cert = db.get(Certificate, certificate_id)
    if cert is None:
        raise HTTPException(404, "Certificate not found")
    return FileResponse(_safe_file(request, cert), media_type="application/pdf", filename=_filename(cert))


# Public: the QR code on every certificate points here.
@public_router.get("/certificates/verify/{certificate_number}", response_model=VerificationOut)
def verify_certificate(certificate_number: str, db: Session = Depends(get_db)):
    cert = db.scalar(select(Certificate).where(
        Certificate.certificate_number == certificate_number,
        Certificate.status == CertStatus.COMPLETED.value))
    if cert is None:
        raise HTTPException(404, "No valid certificate with that number")
    job = cert.job
    return VerificationOut(valid=True, certificate_number=cert.certificate_number,
                           recipient_name=cert.recipient_name or "", event_name=job.event_name,
                           issuer=job.issuer, issue_date=job.issue_date)