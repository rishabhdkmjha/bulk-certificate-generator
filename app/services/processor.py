"""Background processing.

Design: the DB is the source of truth (no in-memory queue state). A job is submitted to a
small thread pool; each certificate is generated and committed independently, so
  * one failure never blocks the others,
  * progress is visible after every certificate,
  * after a crash/restart `recover()` re-queues unfinished jobs and only the certificates
    that are still PENDING/PROCESSING get (re)generated.

Run ONE server process (no `uvicorn --workers N`): `recover()` runs at startup in every
process, so several would all pick up the same unfinished jobs. Scale out with a
broker-backed queue instead (see README).
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Certificate, CertStatus, ErrorType, Job, JobStatus, utcnow
from app.services.generator import CertificateData
from app.services.jobs import finalize_job

log = logging.getLogger("certgen")

Generator = Callable[[CertificateData, Path], None]


def process_job(session_factory: sessionmaker[Session], generator: Generator,
                storage_dir: Path, verify_base_url: str, job_id: str,
                should_stop: Callable[[], bool] = lambda: False) -> None:
    with session_factory() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = JobStatus.PROCESSING.value
        job.started_at = job.started_at or utcnow()
        event_name, issuer, issue_date = job.event_name, job.issuer, job.issue_date
        cert_ids = db.scalars(
            select(Certificate.id)
            .where(Certificate.job_id == job_id,
                   Certificate.status.in_([CertStatus.PENDING.value, CertStatus.PROCESSING.value]))
            .order_by(Certificate.row_index)
        ).all()
        db.commit()

    for cert_id in cert_ids:
        if should_stop():
            # Graceful shutdown: leave the job PROCESSING; recover() resumes it on next start.
            log.info("job %s: stopping early, will resume on next startup", job_id)
            return
        try:
            _process_one(session_factory, generator, storage_dir, verify_base_url,
                         job_id, cert_id, event_name, issuer, issue_date)
        except Exception as exc:  # infrastructure error (DB/disk)
            log.exception("job %s: could not persist result for certificate %s", job_id, cert_id)
            _mark_failed_best_effort(session_factory, cert_id, exc)

    with session_factory() as db:
        finalize_job(db, job_id)


def _mark_failed_best_effort(session_factory, cert_id: str, exc: Exception) -> None:
    """Don't leave a certificate stuck in PROCESSING (which would keep the job open forever)."""
    try:
        with session_factory() as db:
            cert = db.get(Certificate, cert_id)
            if cert is not None and cert.status != CertStatus.COMPLETED.value:
                cert.status = CertStatus.FAILED.value
                cert.error_type = ErrorType.GENERATION.value
                cert.error = f"internal error: {exc.__class__.__name__}"
                db.commit()
    except Exception:  # DB itself is down - recover() will pick the row up on restart
        log.exception("could not mark certificate %s as failed", cert_id)


def _process_one(session_factory, generator, storage_dir: Path, verify_base_url: str,
                 job_id, cert_id, event_name, issuer, issue_date) -> None:
    with session_factory() as db:
        cert = db.get(Certificate, cert_id)
        if cert is None or cert.status == CertStatus.COMPLETED.value:
            return
        cert.status = CertStatus.PROCESSING.value
        db.commit()

        rel_path = f"{job_id}/{cert.certificate_number}.pdf"
        final = storage_dir / rel_path
        tmp = final.with_name(f"{final.stem}.{uuid.uuid4().hex[:8]}.pdf.tmp")  # unique per attempt
        data = CertificateData(
            certificate_number=cert.certificate_number,
            recipient_name=cert.recipient_name or "",
            achievement=cert.achievement or "Participation",
            event_name=event_name, issuer=issuer, issue_date=issue_date,
            verify_url=f"{verify_base_url}/api/v1/certificates/verify/{cert.certificate_number}",
        )
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            generator(data, tmp)
            os.replace(tmp, final)  # atomic: a download never sees a half-written PDF
            cert.status = CertStatus.COMPLETED.value
            cert.file_path = rel_path
            cert.generated_at = utcnow()
            cert.error = cert.error_type = None
        except Exception as exc:  # isolate: this certificate fails, the job carries on
            tmp.unlink(missing_ok=True)
            log.warning("job %s: certificate %s failed: %s", job_id, cert.certificate_number, exc)
            cert.status = CertStatus.FAILED.value
            cert.error_type = ErrorType.GENERATION.value
            cert.error = str(exc)[:500] or exc.__class__.__name__
        db.commit()


class JobDispatcher:
    def __init__(self, session_factory, generator: Generator, storage_dir: Path,
                 verify_base_url: str, workers: int = 2, sync: bool = False):
        self._args = (session_factory, generator, storage_dir, verify_base_url)
        self._sf = session_factory
        self._sync = sync
        self._stop = threading.Event()
        self._pool = None if sync else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="certgen")

    def submit(self, job_id: str) -> None:
        if self._sync:
            process_job(*self._args, job_id)
        else:
            self._pool.submit(self._safe_run, job_id)

    def _safe_run(self, job_id: str) -> None:
        try:
            process_job(*self._args, job_id, should_stop=self._stop.is_set)
        except Exception:
            log.exception("job %s crashed; it will be retried on next startup", job_id)

    def recover(self) -> int:
        """Re-queue jobs that were interrupted (server restart / crash)."""
        with self._sf() as db:
            ids = db.scalars(select(Job.id).where(
                Job.status.in_([JobStatus.QUEUED.value, JobStatus.PROCESSING.value]))).all()
        for job_id in ids:
            self.submit(job_id)
        return len(ids)

    def shutdown(self) -> None:
        """Stop promptly: finish the certificate in flight, drop queued jobs.

        Nothing is lost - the database holds all state and recover() resumes on next start.
        (Waiting for every queued job would make a deploy/restart hang for minutes.)
        """
        self._stop.set()
        if self._pool:
            self._pool.shutdown(wait=True, cancel_futures=True)
