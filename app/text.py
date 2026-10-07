"""Text cleaning shared by request validation (schemas) and the PDF generator."""
from __future__ import annotations

import re
import unicodedata

# Zero-width joiner / non-joiner are legitimate in some scripts, so they are allowed.
_ALLOWED_FORMAT_CHARS = {"‌", "‍"}
_WHITESPACE = re.compile(r"\s+")


def clean_text(value: str) -> str:
    """Normalise user text for printing on a certificate.

    * NFC-normalise, so "e" + combining accent becomes the single character "é"
      (data copied from macOS / PDFs often arrives decomposed and would otherwise be
      rejected by the font check below).
    * Reject control characters (Cc) and invisible/bidi format characters (Cf) such as
      U+202E (right-to-left override) or U+200B (zero-width space) - they can make a
      printed name read differently from what was submitted.
    * Collapse runs of whitespace to a single space.
    """
    value = unicodedata.normalize("NFC", value)
    for ch in value:
        cat = unicodedata.category(ch)
        if ch in "\t\n\r":  # whitespace controls are collapsed below, not rejected
            continue
        if cat == "Cc" or (cat == "Cf" and ch not in _ALLOWED_FORMAT_CHARS):
            raise ValueError("must not contain control or invisible formatting characters")
    return _WHITESPACE.sub(" ", value).strip()