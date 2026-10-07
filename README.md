# Bulk Certificate Generator

A backend API that generates certificates for a whole list of people in one go. You send a single request with all the recipients, it gives you a job id straight away, and you can check on the job and download the PDFs whenever they are ready.

I built it with Python, FastAPI, SQLAlchemy (SQLite by default, PostgreSQL works too) and ReportLab for drawing the PDFs.

## Setup

You need Python 3.10 or newer.

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

Mac / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

`requirements.txt` has only what the app needs. `requirements-dev.txt` adds the test tools.

## Running the app

```bash
uvicorn app.main:create_app --factory --reload
```

The API is now at http://localhost:8000 and the Swagger docs, where you can try every endpoint from the browser, are at http://localhost:8000/docs.

The database and the generated PDFs are stored in a `data/` folder next to the code. It is created on first run.

There is also a Dockerfile if you prefer that:

```bash
docker build -t certgen .
docker run -p 8000:8000 -v certdata:/srv/data certgen
```

### Settings

Everything has a default, so none of this is required. Set them as environment variables if you want to change something.

| Variable | Default | What it does |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/certificates.db` | Any SQLAlchemy URL, e.g. `postgresql+psycopg://user:pw@host/db` |
| `STORAGE_DIR` | `./data/certificates` | Where the PDFs are saved |
| `MAX_RECIPIENTS_PER_JOB` | `1000` | Bigger requests are rejected with 413 |
| `WORKER_THREADS` | `2` | How many jobs can run at the same time |
| `SYNC_PROCESSING` | `false` | If `true`, certificates are generated inside the request itself |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | The address put into each certificate's QR code |
| `FONT_PATH` | not set | A `.ttf` file, needed for names outside Latin-1 |
| `API_KEY` | not set | If set, requests must send an `X-API-Key` header |

## Running the tests

```bash
pytest
```

There are 53 tests. They cover creating a job, request and per-recipient validation, the PDF contents, status and progress, a single certificate failing without stopping the rest, retrieving certificates (one, a list, a ZIP), retrying failures, crash recovery, real background processing, the API key and the file path check. `tests/test_hardening.py` holds regression tests for bugs I found while reviewing my own code.

## Using the API

| Method | Path | What it does |
|---|---|---|
| POST | `/api/v1/jobs` | Submit a bulk request |
| GET | `/api/v1/jobs` | List jobs (`?status=`, `?limit=`, `?offset=`) |
| GET | `/api/v1/jobs/{job_id}` | Status, progress counts and the first 100 failures |
| GET | `/api/v1/jobs/{job_id}/certificates` | Result for every recipient (`?status=failed` etc.) |
| GET | `/api/v1/jobs/{job_id}/download` | ZIP of all certificates generated so far |
| POST | `/api/v1/jobs/{job_id}/retry` | Try the failed certificates again |
| GET | `/api/v1/certificates/{certificate_id}/download` | A single PDF |
| GET | `/api/v1/certificates/verify/{certificate_number}` | Public check, this is what the QR code opens |

### Submitting a request

`sample_request.json` in the repo is ready to use. Its last recipient is invalid on purpose so you can see how failures are reported.

```bash
curl -X POST http://localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d @sample_request.json
```

PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/jobs `
  -ContentType "application/json" -InFile sample_request.json
```

The body looks like this:

```json
{
  "event_name": "PyCon India 2026",
  "issuer": "Python Society of India",
  "issue_date": "2026-10-07",
  "recipients": [
    { "name": "Asha Verma", "email": "asha@example.com" },
    { "name": "Rahul Singh", "email": "rahul@example.com", "achievement": "Excellence" }
  ]
}
```

Only `event_name` and `recipients` are required. `issuer` defaults to "The Organizing Committee", `issue_date` to today (UTC), and each recipient's `achievement` to "Participation".

You get back `202 Accepted` with the job. Trimmed down it looks like:

```json
{
  "job_id": "6f1c...",
  "status": "queued",
  "progress": { "total": 4, "completed": 0, "failed": 1, "pending": 3, "processing": 0, "percent_done": 25.0 },
  "failures": [
    { "row_index": 3, "recipient_name": "", "recipient_email": "invalid-row@example.com",
      "error_type": "validation", "error": "name: String should have at least 1 character" }
  ]
}
```

Invalid rows already show up as failed here, because they are checked while the request is being saved.

### Checking progress

`GET /api/v1/jobs/{job_id}` returns the job. The status goes from `queued` to `processing` and ends as one of:

- `completed`: every certificate was generated
- `completed_with_errors`: some worked, some did not, see `failures`
- `failed`: nothing could be generated

Each failure has a `row_index` (the position in the list you sent), the recipient's name and email, an `error_type` (`validation` or `generation`) and a readable message.

### Getting the certificates

```bash
# list them, optionally filtered by status
curl "http://localhost:8000/api/v1/jobs/<job_id>/certificates?status=completed"

# one PDF, using the id from the list
curl -OJ http://localhost:8000/api/v1/certificates/<certificate_id>/download

# everything as a ZIP
curl -OJ http://localhost:8000/api/v1/jobs/<job_id>/download
```

The ZIP works while a job is still running, it just contains whatever is finished so far.

### Retrying

If some certificates failed while being drawn (`error_type` is `generation`), for example because of a temporary disk problem, call `POST /api/v1/jobs/{job_id}/retry`. Only those certificates are tried again. Validation failures are not retried, since the data itself was wrong. In that case send the corrected rows as a new job.

For a load test there is a small script: `python scripts/make_sample_request.py 500 > bulk_500.json`.

## Design decisions

**Background processing instead of doing it inside the request.** Drawing one PDF takes about 35 ms on my machine, so 1000 certificates is more than half a minute. Holding an HTTP request open that long invites timeouts and client retries that would create duplicate jobs. So `POST /jobs` validates everything, saves it, returns 202, and a worker thread does the generation. The client polls the job. I kept a `SYNC_PROCESSING` switch, which the tests use so results are predictable.

**A thread pool instead of Celery and Redis.** For this project I wanted setup to be just `pip install` and `uvicorn`. To make that safe, the database is the source of truth: every certificate has its own status and is committed on its own. If the server stops halfway, the next start looks for unfinished jobs and only generates the certificates that are still pending, so nothing is lost and nothing is generated twice. Stopping the server is also quick, since it finishes the certificate it is on and leaves the rest for the next start. The worker function `process_job(job_id)` is a plain function, so moving to Celery later means calling it from a task instead of from the thread pool.

**Validation happens at two levels.** Problems with the request as a whole (no `event_name`, an empty list, a bad date) return 422 for the entire request. Problems with a single recipient (bad email, empty name, a duplicate email in the same job, a row that is not an object) do not reject the request. That row is saved as failed with a reason and the valid ones carry on. I typed the recipient list loosely at the request level for exactly this reason, so one bad row cannot cause a 422 for thousands of good ones. Names are normalised (so accented letters typed as two characters become one) and invisible or control characters are rejected, because the printed name should read exactly as it was submitted.

**The event name and issuer are checked up front.** These are shared by every certificate in a job. If they cannot be printed (too long even after wrapping onto two lines, or characters the font cannot draw), every single certificate would fail with the same error. So the request is rejected with a clear 422 before anything is created.

**A failure in one certificate does not affect the others.** Each certificate is generated in its own try/except and its own transaction. The PDF is first written to a temporary file and then renamed, so a crash never leaves a half-written PDF and a download never sees one. The job status is worked out from the per-certificate counts each time instead of being kept in counters, so it cannot drift out of sync.

**One template, and a font limitation.** There is a single landscape A4 template drawn with ReportLab: a border, the name shrunk to fit, the event, date, issuer, a unique certificate number and a QR code that links to the verify endpoint. ReportLab's built-in fonts only cover Latin-1, so a name outside that (Hindi, for example) fails for that one certificate with a clear message instead of printing garbage. Setting `FONT_PATH` to a Unicode `.ttf` fixes it for scripts that font supports. Complex scripts like Devanagari may still not join letters perfectly, because ReportLab does not do text shaping.

**Smaller things.**
- Lists are paginated. The job status shows at most 100 failures, and the full list is at `/certificates?status=failed`.
- PDFs are stored as `STORAGE_DIR/<job_id>/<certificate_number>.pdf`. The database keeps only the relative path, and downloads refuse any path that points outside the storage folder.
- The ZIP is built on disk rather than in memory and skips any file that has gone missing instead of failing the whole download.
- The API key is optional. The verify endpoint stays public so the QR code works for anyone.
- The app is created by a `create_app(settings, generator)` function, which lets the tests use a temporary database and a fake generator.
- SQLite runs in WAL mode so polling for status does not block the worker.

## Limitations

- Run only one server process. Do not use `uvicorn --workers N`, because each process would try to resume the same unfinished jobs on startup.
- Tables are created with `create_all`. A real deployment should use migrations (Alembic).
- PDFs are on local disk. With more than one server they would need to be in object storage like S3.
- The thread pool only scales on one machine. After that a proper queue (Celery, RQ) is the next step.
- Duplicate emails inside one job are rejected. That was my choice, and it is easy to change in `app/services/jobs.py`.
- There is no idempotency key, so a client that retries a timed-out `POST /jobs` creates a second job.
- Emails are validated and stored but nothing is sent by email.

## Project layout

```
app/
  main.py          app setup and startup recovery
  api.py           the routes
  schemas.py       request and response models, per-recipient validation
  models.py        Job and Certificate tables
  config.py        settings from environment variables
  text.py          text cleaning used by validation
  deps.py          database session and API key check
  services/
    jobs.py        creating jobs, validating rows, progress, retry
    processor.py   background worker and crash recovery
    generator.py   the PDF template
tests/             pytest tests
scripts/           helper to build large sample requests
```