from datetime import date

import pytest
from pypdf import PdfReader

from app.services.generator import CertificateData, CertificateGenerationError, PdfCertificateGenerator


def _data(name="Asha Verma", **kw):
    base = dict(certificate_number="CERT-AAAAA-BBBBB", recipient_name=name,
                achievement="Participation", event_name="PyCon India 2026",
                issuer="Python Society", issue_date=date(2026, 10, 7),
                verify_url="http://localhost:8000/api/v1/certificates/verify/CERT-AAAAA-BBBBB")
    base.update(kw)
    return CertificateData(**base)


def test_generates_valid_pdf_containing_recipient_data(tmp_path):
    out = tmp_path / "c.pdf"
    PdfCertificateGenerator()(_data(), out)
    assert out.read_bytes().startswith(b"%PDF")
    text = PdfReader(str(out)).pages[0].extract_text()
    for expected in ("Asha Verma", "PyCon India 2026", "Python Society", "CERT-AAAAA-BBBBB", "07 October 2026"):
        assert expected in text


def test_long_name_is_shrunk_to_fit(tmp_path):
    PdfCertificateGenerator()(_data(name="Alexandrina " * 7), tmp_path / "c.pdf")


def test_unsupported_characters_raise_clear_error(tmp_path):
    with pytest.raises(CertificateGenerationError, match="FONT_PATH"):
        PdfCertificateGenerator()(_data(name="ऋषभ शर्मा"), tmp_path / "c.pdf")


def test_unfittable_name_raises(tmp_path):
    with pytest.raises(CertificateGenerationError, match="too long"):
        PdfCertificateGenerator()(_data(name="W" * 100), tmp_path / "c.pdf")
