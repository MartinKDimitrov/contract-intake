"""PDF inspection.

Two levels, deliberately separated by cost:

* ``probe`` reads structure only -- page count, encryption, whether page one
  carries a text layer. Cheap enough for triage to run on every file.
* Full page loading lands in phase 3, where the per-page text-versus-vision
  decision is made.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

log = logging.getLogger(__name__)

#: Beyond this a "contract" is almost certainly a bundle or a scan dump. Triage
#: sends it to review rather than paying to read all of it.
MAX_REASONABLE_PAGES = 120


@dataclass(frozen=True, slots=True)
class PdfProbe:
    # fmt: off
    readable        : bool
    encrypted       : bool
    page_count      : int
    first_page_text : str
    error           : str | None = None
    # fmt: on

    @property
    def has_text_layer(self) -> bool:
        return len(self.first_page_text.strip()) >= 40


def probe(path: Path, *, sample_chars: int = 4000) -> PdfProbe:
    """Inspect a PDF without extracting all of it.

    Never raises: a corrupt or encrypted file is an expected input here, and the
    caller decides what to do about it.
    """
    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = pdf.pages
            first = pages[0].extract_text() or "" if pages else ""
            return PdfProbe(
                readable=True,
                encrypted=False,
                page_count=len(pages),
                first_page_text=first[:sample_chars],
            )
    except Exception as exc:  # pdfplumber raises a wide variety on damaged input
        message = str(exc)
        encrypted = "password" in message.lower() or "encrypt" in message.lower()
        log.info("pdf probe failed for %s: %s", path.name, message)
        return PdfProbe(
            readable=False,
            encrypted=encrypted,
            page_count=0,
            first_page_text="",
            error=message,
        )
