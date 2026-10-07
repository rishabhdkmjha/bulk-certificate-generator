"""PDF certificate rendering (ReportLab - free, pure Python, no system deps)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

NAVY = HexColor("#1B2A49")
GOLD = HexColor("#B8860B")
CREAM = HexColor("#FFFDF5")
GREY = HexColor("#6B7280")

W_TEXT = landscape(A4)[0] - 200   # printable width for centred text blocks
ISSUER_WIDTH = 260                # width of each footer signature block


class CertificateGenerationError(Exception):
    """Raised for problems specific to one certificate (bad glyphs, name too long...)."""


@dataclass(frozen=True)
class CertificateData:
    certificate_number: str
    recipient_name: str
    achievement: str
    event_name: str
    issuer: str
    issue_date: date
    verify_url: str


class PdfCertificateGenerator:
    """Single predefined template. Callable: generator(data, output_path)."""

    def __init__(self, font_path: str | None = None):
        self._custom_font = False
        if font_path:
            pdfmetrics.registerFont(TTFont("CertFont", font_path))
            self._custom_font = True
            self.regular = self.bold = self.italic = "CertFont"
        else:
            self.regular, self.bold, self.italic = "Helvetica", "Helvetica-Bold", "Times-BoldItalic"

    # -- helpers ---------------------------------------------------------
    def _ensure_supported(self, label: str, text: str) -> None:
        if self._custom_font:
            # A TTF silently draws a "missing glyph" box for characters it lacks, so check.
            glyphs = pdfmetrics.getFont("CertFont").face.charToGlyph
            missing = sorted({ch for ch in text if not ch.isspace() and ord(ch) not in glyphs})
            if missing:
                raise CertificateGenerationError(
                    f"{label} contains characters missing from the configured font: "
                    + " ".join(missing[:5])
                )
            return
        try:
            text.encode("cp1252")  # what ReportLab's built-in fonts can draw
        except UnicodeEncodeError:
            raise CertificateGenerationError(
                f"{label} contains characters the default font cannot render; "
                "set FONT_PATH to a Unicode .ttf font"
            ) from None

    @staticmethod
    def _fit(text: str, font: str, size: float, max_width: float, min_size: float, label: str) -> float:
        while pdfmetrics.stringWidth(text, font, size) > max_width:
            size -= 1
            if size < min_size:
                raise CertificateGenerationError(f"{label} is too long to fit on the certificate")
        return size

    @staticmethod
    def _fit_wrapped(text: str, font: str, start: int, min_size: int, max_width: float,
                     max_lines: int, label: str) -> tuple[int, list[str]]:
        """Largest font size at which `text` fits in <= max_lines lines of max_width."""
        for size in range(start, min_size - 1, -1):
            lines = simpleSplit(text, font, size, max_width)
            if len(lines) <= max_lines and all(
                    pdfmetrics.stringWidth(ln, font, size) <= max_width for ln in lines):
                return size, lines
        raise CertificateGenerationError(f"{label} is too long to fit on the certificate")

    @staticmethod
    def _draw_qr(c: canvas.Canvas, url: str, x: float, y: float, size: float) -> None:
        widget = qr.QrCodeWidget(url)
        x0, y0, x1, y1 = widget.getBounds()
        d = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
        d.add(widget)
        renderPDF.draw(d, c, x, y)

    # Layout of the two job-wide text blocks. Shared by rendering and by validate_job_fields()
    # so "will it fit?" is answered by exactly the same code that draws it.
    def _event_layout(self, text: str) -> tuple[int, list[str]]:
        return self._fit_wrapped(text, self.bold, 24, 12, W_TEXT, 2, "event name")

    def _issuer_layout(self, text: str) -> tuple[int, list[str]]:
        return self._fit_wrapped(text, self.regular, 13, 9, ISSUER_WIDTH, 2, "issuer")

    def validate_job_fields(self, event_name: str, issuer: str) -> None:
        """Raise CertificateGenerationError if the job-wide fields can't be printed.

        Called when a job is submitted: otherwise an unprintable event name would make
        *every* certificate in the job fail one by one.
        """
        self._ensure_supported("event name", event_name)
        self._ensure_supported("issuer", issuer)
        self._event_layout(event_name)
        self._issuer_layout(issuer)

    # -- rendering -------------------------------------------------------
    def __call__(self, data: CertificateData, output_path: Path) -> None:
        for label, text in [("name", data.recipient_name), ("achievement", data.achievement),
                            ("event name", data.event_name), ("issuer", data.issuer)]:
            self._ensure_supported(label, text)

        W, H = landscape(A4)
        c = canvas.Canvas(str(output_path), pagesize=(W, H))
        c.setTitle(f"Certificate - {data.recipient_name}")
        c.setAuthor(data.issuer)

        # Background + double border
        c.setFillColor(CREAM)
        c.rect(0, 0, W, H, stroke=0, fill=1)
        c.setStrokeColor(NAVY); c.setLineWidth(6); c.rect(24, 24, W - 48, H - 48)
        c.setStrokeColor(GOLD); c.setLineWidth(1.5); c.rect(36, 36, W - 72, H - 72)

        # Heading
        c.setFillColor(NAVY); c.setFont(self.bold, 40)
        c.drawCentredString(W / 2, H - 110, "CERTIFICATE")
        c.setFillColor(GOLD); c.setFont(self.regular, 15)
        sub = self._fit(f"OF {data.achievement.upper()}", self.regular, 15, W - 200, 9, "achievement")
        c.setFont(self.regular, sub)
        c.drawCentredString(W / 2, H - 138, f"OF {data.achievement.upper()}")

        # Recipient
        c.setFillColor(GREY); c.setFont(self.regular, 12)
        c.drawCentredString(W / 2, H - 190, "This certificate is proudly presented to")
        name_size = self._fit(data.recipient_name, self.italic, 44, W - 200, 12, "recipient name")
        c.setFillColor(NAVY); c.setFont(self.italic, name_size)
        c.drawCentredString(W / 2, H - 245, data.recipient_name)
        c.setStrokeColor(GOLD); c.setLineWidth(1)
        c.line(W / 2 - 230, H - 258, W / 2 + 230, H - 258)

        # Event
        c.setFillColor(GREY); c.setFont(self.regular, 12)
        c.drawCentredString(W / 2, H - 292, "in recognition of their achievement at")
        ev_size, ev_lines = self._event_layout(data.event_name)
        c.setFillColor(NAVY); c.setFont(self.bold, ev_size)
        for i, line in enumerate(ev_lines):
            c.drawCentredString(W / 2, H - 326 - i * ev_size * 1.2, line)

        # Signature-style footer
        y = 128
        c.setStrokeColor(NAVY); c.setLineWidth(0.8)
        c.line(70, y, 330, y); c.line(W - 330, y, W - 70, y)
        c.setFillColor(NAVY); c.setFont(self.regular, 13)
        c.drawCentredString(200, y + 8, data.issue_date.strftime("%d %B %Y"))
        iss_size, iss_lines = self._issuer_layout(data.issuer)
        c.setFont(self.regular, iss_size)
        for i, line in enumerate(reversed(iss_lines)):  # extra lines stack upwards
            c.drawCentredString(W - 200, y + 8 + i * iss_size * 1.2, line)
        c.setFillColor(GREY); c.setFont(self.regular, 9)
        c.drawCentredString(200, y - 14, "DATE OF ISSUE")
        c.drawCentredString(W - 200, y - 14, "ISSUED BY")

        # QR + serial number for verification
        self._draw_qr(c, data.verify_url, W / 2 - 32, 62, 64)
        c.setFont(self.regular, 8); c.setFillColor(GREY)
        c.drawCentredString(W / 2, 48, f"No. {data.certificate_number}  |  Scan to verify")

        c.showPage()
        c.save()
