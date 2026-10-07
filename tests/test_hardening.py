"""Regression tests for bugs found in code review.

Each test names the failure it guards against.
"""
import io
import os
import unicodedata
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfReader

from app.config import Settings
from app.models import Certificate
from app.services import processor
from app.services.generator import (CertificateData, CertificateGenerationError,
                                    PdfCertificateGenerator)
from app.services.processor import process_job
from tests.conftest import GOOD, job_payload

DEJAVU = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
LONG_EVENT = ("International Conference on Applied Machine Learning and Data Engineering "
              "for Sustainable Smart Cities 2026 - New Delhi Edition, Hybrid Format")


def _certs(client, job):
    return client.get(job["links"]["certificates"]).json()["items"]


# ---------- API key -----------------------------------------------------------
def test_non_ascii_api_key_is_401_not_500(make_client):
    # secrets.compare_digest(str, str) raises TypeError on non-ASCII -> used to be a 500.
    c = make_client(api_key="secret")
    r = c.get("/api/v1/jobs", headers=[(b"x-api-key", "café".encode("latin-1"))])
    assert r.status_code == 401


# ---------- text cleaning / validation ---------------------------------------
def test_decomposed_unicode_name_is_normalised_and_generated(client):
    nfd = unicodedata.normalize("NFD", "José Álvarez")  # e + combining accent
    assert nfd != "José Álvarez"
    job = client.post("/api/v1/jobs", json=job_payload([{"name": nfd, "email": "j@example.com"}])).json()
    assert job["status"] == "completed"
    assert _certs(client, job)[0]["recipient_name"] == "José Álvarez"


@pytest.mark.parametrize("name", ["Ann‮Bob", "Zero​Width", "Null\x00Byte"])
def test_invisible_and_control_characters_rejected_at_validation(client, name):
    job = client.post("/api/v1/jobs", json=job_payload(
        [{"name": name, "email": "x@example.com"}])).json()
    [f] = job["failures"]
    assert f["error_type"] == "validation" and "control or invisible" in f["error"]


def test_whitespace_inside_name_is_collapsed(client):
    job = client.post("/api/v1/jobs", json=job_payload(
        [{"name": "  Asha \t  Verma  ", "email": "a@example.com"}])).json()
    assert _certs(client, job)[0]["recipient_name"] == "Asha Verma"


# ---------- job-wide fields: fail fast, wrap long text ------------------------
def test_long_event_name_is_wrapped_and_renders(client):
    assert len(LONG_EVENT) > 120
    job = client.post("/api/v1/jobs", json=job_payload(GOOD, event_name=LONG_EVENT)).json()
    assert job["status"] == "completed"
    pdf = client.get(_certs(client, job)[0]["download_url"]).content
    text = " ".join(PdfReader(io.BytesIO(pdf)).pages[0].extract_text().split())
    assert "International Conference" in text and "Hybrid Format" in text


@pytest.mark.parametrize("field,value", [
    ("event_name", "W" * 140),            # cannot fit on the template at any size
    ("event_name", "भारत सम्मेलन"),         # glyphs the default font lacks
    ("issuer", "W" * 90),
])
def test_unprintable_job_fields_return_422_and_create_no_job(client, field, value):
    r = client.post("/api/v1/jobs", json=job_payload(GOOD, **{field: value}))
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", field]
    assert client.get("/api/v1/jobs").json()["total"] == 0  # nothing half-created


def test_long_issuer_wraps(client):
    issuer = "Department of Computer Science and Engineering, Indira Gandhi Technical University"
    job = client.post("/api/v1/jobs", json=job_payload(GOOD[:1], issuer=issuer)).json()
    assert job["status"] == "completed"


# ---------- stored raw input is bounded ---------------------------------------
def test_raw_input_is_bounded_and_json_safe(client):
    row = {"name": "Big", "email": "big@example.com", "junk": "A" * 100_000,
           "nested": {"a": [1, 2, 3]}}
    job = client.post("/api/v1/jobs", json=job_payload([row])).json()
    with client.app.state.session_factory() as db:
        raw = db.query(Certificate).filter_by(job_id=job["job_id"]).one().raw_input
    assert len(raw["junk"]) == 300 and raw["nested"] == "<dict>"


def test_nan_in_row_does_not_break_storage(client):
    body = '{"event_name":"E","recipients":[{"name":"N","email":"n@example.com","x":NaN}]}'
    r = client.post("/api/v1/jobs", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 202


# ---------- ZIP download -------------------------------------------------------
def test_zip_skips_a_file_missing_on_disk(client):
    job = client.post("/api/v1/jobs", json=job_payload(GOOD)).json()
    first = _certs(client, job)[0]
    with client.app.state.session_factory() as db:
        rel = db.get(Certificate, first["certificate_id"]).file_path
    os.remove(client.app.state.settings.storage_dir / rel)
    r = client.get(job["links"]["download_all"])
    assert r.status_code == 200
    assert len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == 2


def test_zip_when_every_file_is_missing_is_404(client):
    job = client.post("/api/v1/jobs", json=job_payload(GOOD[:1])).json()
    for f in client.app.state.settings.storage_dir.rglob("*.pdf"):
        f.unlink()
    assert client.get(job["links"]["download_all"]).status_code == 404


def test_zip_temp_file_is_cleaned_up(client, tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "ziptmp"))
    (tmp_path / "ziptmp").mkdir()
    job = client.post("/api/v1/jobs", json=job_payload(GOOD)).json()
    assert client.get(job["links"]["download_all"]).status_code == 200
    assert list((tmp_path / "ziptmp").glob("*.zip")) == []


# ---------- retry ---------------------------------------------------------------
class FlakyGenerator(PdfCertificateGenerator):
    fail = True

    def __call__(self, data, output_path):
        if self.fail and data.recipient_name == "Flaky Person":
            raise RuntimeError("disk hiccup")
        super().__call__(data, output_path)


def test_retry_regenerates_only_generation_failures(make_client):
    gen = FlakyGenerator()
    c = make_client(generator=gen)
    rows = [GOOD[0], {"name": "Flaky Person", "email": "f@example.com"}, {"name": "Bad"}]
    job = c.post("/api/v1/jobs", json=job_payload(rows)).json()
    assert job["status"] == "completed_with_errors" and job["progress"]["failed"] == 2

    gen.fail = False  # the transient problem is gone
    r = c.post(f"/api/v1/jobs/{job['job_id']}/retry")
    assert r.status_code == 202
    body = c.get(f"/api/v1/jobs/{job['job_id']}").json()
    assert body["progress"]["completed"] == 2 and body["progress"]["failed"] == 1
    assert [f["error_type"] for f in body["failures"]] == ["validation"]  # not retried
    assert body["status"] == "completed_with_errors"


def test_retry_with_nothing_to_retry_is_409(client):
    done = client.post("/api/v1/jobs", json=job_payload(GOOD)).json()
    assert client.post(f"/api/v1/jobs/{done['job_id']}/retry").status_code == 409
    invalid = client.post("/api/v1/jobs", json=job_payload([{"name": "Bad"}])).json()
    assert client.post(f"/api/v1/jobs/{invalid['job_id']}/retry").status_code == 409


def test_retry_while_job_is_running_is_409(client):
    client.app.state.dispatcher.submit = lambda job_id: None  # job stays queued
    job = client.post("/api/v1/jobs", json=job_payload(GOOD)).json()
    assert client.post(f"/api/v1/jobs/{job['job_id']}/retry").status_code == 409


def test_retry_unknown_job_is_404(client):
    assert client.post("/api/v1/jobs/nope/retry").status_code == 404


# ---------- shutdown / recovery ---------------------------------------------------
def test_graceful_stop_leaves_job_resumable(client):
    client.app.state.dispatcher.submit = lambda job_id: None
    job = client.post("/api/v1/jobs", json=job_payload(GOOD)).json()
    st = client.app.state
    process_job(st.session_factory, PdfCertificateGenerator(), st.settings.storage_dir,
                st.settings.public_base_url, job["job_id"], should_stop=lambda: True)
    body = client.get(f"/api/v1/jobs/{job['job_id']}").json()
    assert body["status"] == "processing" and body["progress"]["completed"] == 0

    del client.app.state.dispatcher.submit  # restore the real method
    assert client.app.state.dispatcher.recover() == 1
    assert client.get(f"/api/v1/jobs/{job['job_id']}").json()["status"] == "completed"


def test_infrastructure_error_does_not_leave_job_stuck(client, monkeypatch):
    real = processor._process_one

    def flaky(sf, gen, storage, base, job_id, cert_id, *a):
        with sf() as db:
            if db.get(Certificate, cert_id).row_index == 1:
                raise OSError("disk full")
        return real(sf, gen, storage, base, job_id, cert_id, *a)

    monkeypatch.setattr(processor, "_process_one", flaky)
    job = client.post("/api/v1/jobs", json=job_payload(GOOD)).json()
    assert job["status"] == "completed_with_errors"  # finalised, not stuck in "processing"
    [f] = job["failures"]
    assert f["row_index"] == 1 and "internal error" in f["error"]


# ---------- custom font glyph check ---------------------------------------------
@pytest.mark.skipif(not DEJAVU.exists(), reason="DejaVu font not installed")
class TestCustomFont:
    def _data(self, name):
        return CertificateData("CERT-AAAAA-BBBBB", name, "Participation", "Event", "Issuer",
                               __import__("datetime").date(2026, 10, 7), "http://x/verify")

    def test_extended_latin_renders_with_unicode_font(self, tmp_path):
        PdfCertificateGenerator(str(DEJAVU))(self._data("Łukasz Żółć"), tmp_path / "c.pdf")
        assert (tmp_path / "c.pdf").stat().st_size > 1000

    def test_missing_glyphs_are_an_error_not_black_boxes(self, tmp_path):
        with pytest.raises(CertificateGenerationError, match="missing from the configured font"):
            PdfCertificateGenerator(str(DEJAVU))(self._data("ऋषभ"), tmp_path / "c.pdf")


# ---------- settings ----------------------------------------------------------------
@pytest.mark.parametrize("kw", [{"worker_threads": 0}, {"max_recipients_per_job": 0}])
def test_invalid_settings_fail_fast(kw):
    with pytest.raises(ValueError):
        Settings(**kw)


# ---------- root URL ------------------------------------------------------------------
def test_root_redirects_to_docs(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/docs"