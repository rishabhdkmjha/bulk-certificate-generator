import time

import pytest

from app.models import Certificate
from app.services.generator import PdfCertificateGenerator
from app.services.processor import process_job
from tests.conftest import GOOD, job_payload


# ---------- creating a job ---------------------------------------------------
def test_create_job_returns_202_with_location_and_progress(client):
    r = client.post("/api/v1/jobs", json=job_payload(GOOD))
    assert r.status_code == 202
    body = r.json()
    assert r.headers["location"] == f"/api/v1/jobs/{body['job_id']}"
    assert body["progress"]["total"] == 3
    assert body["links"]["certificates"].endswith("/certificates")


# ---------- input validation -------------------------------------------------
@pytest.mark.parametrize("payload", [
    {"issuer": "X", "recipients": GOOD},                              # no event_name
    {"event_name": "   ", "recipients": GOOD},                        # blank event_name
    {"event_name": "E", "recipients": []},                            # empty list
    {"event_name": "E"},                                              # no recipients
    {"event_name": "E", "recipients": GOOD, "issue_date": "nope"},    # bad date
])
def test_request_level_validation_returns_422(client, payload):
    assert client.post("/api/v1/jobs", json=payload).status_code == 422


def test_too_many_recipients_returns_413(client):
    many = [{"name": f"P {chr(65 + i % 26)}", "email": f"p{i}@example.com"} for i in range(51)]
    assert client.post("/api/v1/jobs", json=job_payload(many)).status_code == 413


def test_invalid_rows_are_rejected_individually_valid_rows_still_generated(client):
    rows = [
        {"name": "Good One", "email": "good@example.com"},
        {"name": "", "email": "x@example.com"},                  # empty name
        {"name": "No Email"},                                    # missing email
        {"name": "Bad Email", "email": "not-an-email"},
        {"name": "12345", "email": "digits@example.com"},        # no letters
        {"name": "Good One Again", "email": "GOOD@example.com"}, # duplicate (case-insensitive)
        "just a string",                                         # not an object
        {"name": "Fine Person", "email": "fine@example.com"},
    ]
    body = client.post("/api/v1/jobs", json=job_payload(rows)).json()
    assert body["status"] == "completed_with_errors"
    assert body["progress"] == {"total": 8, "completed": 2, "failed": 6, "pending": 0,
                                "processing": 0, "percent_done": 100.0}
    failed = {f["row_index"]: f for f in body["failures"]}
    assert set(failed) == {1, 2, 3, 4, 5, 6}
    assert all(f["error_type"] == "validation" for f in failed.values())
    assert "duplicate" in failed[5]["error"]
    assert "email" in failed[2]["error"]


def test_job_with_only_invalid_rows_is_failed_immediately(client):
    body = client.post("/api/v1/jobs", json=job_payload([{"name": "A"}, {}])).json()
    assert body["status"] == "failed"
    assert body["finished_at"] is not None
    assert body["progress"]["failed"] == 2


# ---------- certificate generation ------------------------------------------
def test_each_valid_recipient_gets_a_pdf_with_their_details(client):
    from pypdf import PdfReader
    import io
    job = client.post("/api/v1/jobs", json=job_payload(GOOD)).json()
    certs = client.get(f"/api/v1/jobs/{job['job_id']}/certificates").json()["items"]
    assert [c["status"] for c in certs] == ["completed"] * 3
    r = client.get(certs[1]["download_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    text = PdfReader(io.BytesIO(r.content)).pages[0].extract_text()
    assert "Rahul Singh" in text and "EXCELLENCE" in text and "PyCon India 2026" in text
    assert certs[0]["achievement"] == "Participation"  # default applied


# ---------- status / progress ------------------------------------------------
def test_status_progresses_from_queued_to_completed(client):
    client.app.state.dispatcher.submit = lambda job_id: None  # hold the worker back
    job = client.post("/api/v1/jobs", json=job_payload(GOOD)).json()
    assert job["status"] == "queued"
    assert job["progress"]["pending"] == 3 and job["progress"]["percent_done"] == 0.0

    st = client.app.state
    process_job(st.session_factory, PdfCertificateGenerator(), st.settings.storage_dir,
                st.settings.public_base_url, job["job_id"])
    done = client.get(f"/api/v1/jobs/{job['job_id']}").json()
    assert done["status"] == "completed"
    assert done["progress"]["completed"] == 3 and done["progress"]["percent_done"] == 100.0
    assert done["started_at"] and done["finished_at"]


def test_unknown_job_is_404(client):
    assert client.get("/api/v1/jobs/does-not-exist").status_code == 404


def test_background_processing_with_real_worker_thread(make_client):
    c = make_client(sync_processing=False)
    job = c.post("/api/v1/jobs", json=job_payload(GOOD)).json()
    for _ in range(100):
        body = c.get(f"/api/v1/jobs/{job['job_id']}").json()
        if body["status"] == "completed":
            break
        time.sleep(0.1)
    assert body["status"] == "completed" and body["progress"]["completed"] == 3


def test_interrupted_job_is_resumed_by_recover(client):
    real_submit = client.app.state.dispatcher.submit
    client.app.state.dispatcher.submit = lambda job_id: None  # simulate crash before processing
    job = client.post("/api/v1/jobs", json=job_payload(GOOD)).json()
    client.app.state.dispatcher.submit = real_submit
    assert client.app.state.dispatcher.recover() == 1
    assert client.get(f"/api/v1/jobs/{job['job_id']}").json()["status"] == "completed"


# ---------- individual failure handling -------------------------------------
class ExplodingGenerator(PdfCertificateGenerator):
    def __call__(self, data, output_path):
        if data.recipient_name == "Explode Me":
            output_path.write_bytes(b"partial")  # leaves junk behind; must be cleaned up
            raise RuntimeError("renderer crashed")
        super().__call__(data, output_path)


def test_one_generation_failure_does_not_stop_the_rest(make_client):
    c = make_client(generator=ExplodingGenerator())
    rows = [GOOD[0], {"name": "Explode Me", "email": "boom@example.com"}, GOOD[2]]
    job = c.post("/api/v1/jobs", json=job_payload(rows)).json()

    assert job["status"] == "completed_with_errors"
    assert job["progress"]["completed"] == 2 and job["progress"]["failed"] == 1
    [failure] = job["failures"]
    assert failure["row_index"] == 1 and failure["error_type"] == "generation"
    assert "renderer crashed" in failure["error"]

    failed = c.get(f"/api/v1/jobs/{job['job_id']}/certificates?status=failed").json()
    assert failed["total"] == 1 and failed["items"][0]["download_url"] is None
    assert c.get(f"/api/v1/certificates/{failure['certificate_id']}/download").status_code == 409
    assert not list(c.app.state.settings.storage_dir.rglob("*.tmp"))  # no partial files left


def test_unrenderable_name_fails_only_that_certificate(client):
    rows = [GOOD[0], {"name": "ऋषभ शर्मा", "email": "hindi@example.com"}]
    job = client.post("/api/v1/jobs", json=job_payload(rows)).json()
    assert job["status"] == "completed_with_errors"
    assert "FONT_PATH" in job["failures"][0]["error"]


# ---------- retrieving certificates -----------------------------------------
def test_list_filter_and_paginate_certificates(client):
    job = client.post("/api/v1/jobs", json=job_payload(GOOD + [{"name": "Bad"}])).json()
    base = f"/api/v1/jobs/{job['job_id']}/certificates"
    assert client.get(base).json()["total"] == 4
    assert client.get(base + "?status=completed").json()["total"] == 3
    page = client.get(base + "?limit=2&offset=2").json()
    assert [i["row_index"] for i in page["items"]] == [2, 3]
    assert client.get(base + "?status=bogus").status_code == 422


def test_download_zip_contains_only_completed_certificates(client):
    import io, zipfile
    job = client.post("/api/v1/jobs", json=job_payload(GOOD + [{"name": "Bad"}])).json()
    r = client.get(job["links"]["download_all"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert len(names) == 3 and all(n.endswith(".pdf") for n in names)
    assert any("Asha-Verma" in n for n in names)


def test_download_zip_with_nothing_generated_is_409(client):
    job = client.post("/api/v1/jobs", json=job_payload([{"name": "Bad"}])).json()
    assert client.get(job["links"]["download_all"]).status_code == 409


def test_unknown_certificate_is_404(client):
    assert client.get("/api/v1/certificates/nope/download").status_code == 404


def test_verify_certificate_by_number(client):
    job = client.post("/api/v1/jobs", json=job_payload(GOOD)).json()
    cert = client.get(job["links"]["certificates"]).json()["items"][0]
    r = client.get(f"/api/v1/certificates/verify/{cert['certificate_number']}")
    assert r.status_code == 200
    assert r.json()["recipient_name"] == "Asha Verma" and r.json()["valid"] is True
    assert client.get("/api/v1/certificates/verify/CERT-FAKE0-FAKE0").status_code == 404


def test_download_rejects_path_outside_storage(client):
    job = client.post("/api/v1/jobs", json=job_payload(GOOD[:1])).json()
    cert_id = client.get(job["links"]["certificates"]).json()["items"][0]["certificate_id"]
    with client.app.state.session_factory() as db:
        db.get(Certificate, cert_id).file_path = "../../etc/passwd"
        db.commit()
    assert client.get(f"/api/v1/certificates/{cert_id}/download").status_code == 404


# ---------- optional API key -------------------------------------------------
def test_api_key_protects_everything_except_verification(make_client):
    c = make_client(api_key="secret")
    assert c.post("/api/v1/jobs", json=job_payload(GOOD)).status_code == 401
    ok = c.post("/api/v1/jobs", json=job_payload(GOOD), headers={"X-API-Key": "secret"})
    assert ok.status_code == 202
    assert c.get("/api/v1/certificates/verify/CERT-FAKE0-FAKE0").status_code == 404  # reachable, not 401
    assert c.get("/health").status_code == 200
