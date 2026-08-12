"""Triage spends nothing, so its job is turning away obvious non-contracts."""

from __future__ import annotations

import hashlib

import pytest

from contract_intake.db.models import Attachment
from contract_intake.loaders.detect import FileKind, sniff
from contract_intake.loaders.pdf import PdfProbe
from contract_intake.pipeline import stage_02_triage as triage
from contract_intake.pipeline.base import Advanced, Rejected, StageContext
from contract_intake.status import Status

CONTRACT_PAGE = """
MASTER SERVICES AGREEMENT
This Agreement is entered into by and between the parties hereto.
Whereas the Supplier shall provide services, the termination clause and
liability provisions and governing law are set out below.
"""

INVOICE_PAGE = """
INVOICE
Invoice No: 2026-00841       Bill to: Acme Ltd
Subtotal 1000.00   VAT 200.00   Amount due 1200.00
Payment due within 30 days. Remit to IBAN BG00 XXXX.
"""


def _build(settings, name: str, content: bytes, *, email_id: int) -> Attachment:
    settings.ensure_dirs()
    path = settings.attachments_dir / name
    path.write_bytes(content)
    return Attachment(
        email_id=email_id,
        filename=name,
        sha256=hashlib.sha256(name.encode()).hexdigest(),
        declared_mime="application/pdf",
        size_bytes=len(content),
        stored_path=str(path),
        status=Status.RECEIVED,
    )


@pytest.fixture
def write(settings, attachment):
    """Build an attachment on disk, hung off the fixture email."""
    return lambda name, content: _build(settings, name, content, email_id=attachment.email_id)


@pytest.fixture
def run(session, settings):
    async def _run(att: Attachment):
        session.add(att)
        session.flush()
        ctx = StageContext(attachment_id=att.id, session=session, settings=settings)
        return await triage.TriageStage().run(ctx)

    return _run


# -- vocabulary scan (pure, no file needed) ---------------------------------


def test_contract_vocabulary_is_recognised() -> None:
    assert triage.classify_text(CONTRACT_PAGE).kind == "contract"


def test_invoice_is_recognised_and_named() -> None:
    verdict = triage.classify_text(INVOICE_PAGE)
    assert verdict.kind == "invoice"
    assert "invoice" in verdict.evidence


def test_one_stray_word_is_not_enough() -> None:
    assert triage.classify_text("Dear supplier, see you Tuesday.").kind == "unknown"


def test_empty_page_is_unknown_not_contract() -> None:
    assert triage.classify_text("").kind == "unknown"


# -- content sniffing -------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"%PDF-1.7\n", FileKind.PDF),
        (b"\x89PNG\r\n\x1a\n", FileKind.PNG),
        (b"\xff\xd8\xff\xe0", FileKind.JPEG),
        (b"PK\x03\x04" + b"word/document.xml", FileKind.OFFICE),
        (b"MZ\x90\x00", FileKind.UNKNOWN),
    ],
)
def test_type_comes_from_bytes_not_the_declared_header(head: bytes, expected: FileKind) -> None:
    assert sniff(head) == expected


# -- the rejection paths ----------------------------------------------------


async def test_empty_file_is_rejected(run, write) -> None:
    outcome = await run(write("empty.pdf", b""))
    assert isinstance(outcome, Rejected)
    assert "empty" in outcome.reason


async def test_oversized_file_is_rejected(run, write, settings) -> None:
    att = write("big.pdf", b"%PDF-1.7")
    att.size_bytes = (settings.max_attachment_mb + 1) * 1024 * 1024
    outcome = await run(att)
    assert isinstance(outcome, Rejected)
    assert "ceiling" in outcome.reason


async def test_executable_renamed_to_pdf_is_rejected(run, write) -> None:
    outcome = await run(write("invoice.pdf", b"MZ\x90\x00" + b"\x00" * 200))
    assert isinstance(outcome, Rejected)
    assert "unsupported" in outcome.reason


async def test_corrupt_pdf_is_rejected_not_crashed(run, write) -> None:
    outcome = await run(write("truncated.pdf", b"%PDF-1.7\n" + b"\xde\xad\xbe\xef" * 50))
    assert isinstance(outcome, Rejected)
    assert "unreadable" in outcome.reason


async def test_missing_file_on_disk_is_rejected(run, write, settings) -> None:
    att = write("gone.pdf", b"%PDF-1.7")
    att.stored_path = str(settings.attachments_dir / "not-there.pdf")
    outcome = await run(att)
    assert isinstance(outcome, Rejected)
    assert "missing" in outcome.reason


async def test_tiny_image_is_a_signature_not_a_page(run, write) -> None:
    outcome = await run(write("sig.png", b"\x89PNG\r\n\x1a\n" + b"0" * 2000))
    assert isinstance(outcome, Rejected)
    assert "too small" in outcome.reason


async def test_photo_of_a_page_passes_without_a_content_check(run, write) -> None:
    """The messy real-world case must not be thrown away for lack of text."""
    outcome = await run(write("scan.jpg", b"\xff\xd8\xff\xe0" + b"0" * triage.MIN_IMAGE_BYTES))
    assert isinstance(outcome, Advanced)
    assert "stage 04" in outcome.note


# -- the PDF branch, with the probe stubbed ---------------------------------


@pytest.fixture
def stub_probe(monkeypatch):
    def _stub(**kwargs):
        defaults = {
            "readable": True,
            "encrypted": False,
            "page_count": 6,
            "first_page_text": CONTRACT_PAGE,
        }
        monkeypatch.setattr(triage, "probe", lambda _p: PdfProbe(**(defaults | kwargs)))

    return _stub


async def test_encrypted_pdf_is_rejected(run, write, stub_probe) -> None:
    stub_probe(readable=False, encrypted=True, page_count=0, first_page_text="")
    outcome = await run(write("locked.pdf", b"%PDF-1.7"))
    assert isinstance(outcome, Rejected)
    assert "password-protected" in outcome.reason


async def test_bundle_of_pages_is_rejected(run, write, stub_probe) -> None:
    stub_probe(page_count=500)
    outcome = await run(write("bundle.pdf", b"%PDF-1.7"))
    assert isinstance(outcome, Rejected)
    assert "bundle" in outcome.reason


async def test_invoice_pdf_is_turned_away_for_free(run, write, stub_probe) -> None:
    stub_probe(first_page_text=INVOICE_PAGE)
    outcome = await run(write("inv.pdf", b"%PDF-1.7"))
    assert isinstance(outcome, Rejected)
    assert "invoice" in outcome.reason


async def test_scanned_pdf_without_text_layer_advances(run, write, stub_probe) -> None:
    stub_probe(first_page_text="")
    outcome = await run(write("scan.pdf", b"%PDF-1.7"))
    assert isinstance(outcome, Advanced)
    assert "no text layer" in outcome.note


async def test_real_contract_advances_and_records_page_count(run, write, stub_probe) -> None:
    stub_probe(page_count=12)
    outcome = await run(write("msa.pdf", b"%PDF-1.7"))
    assert isinstance(outcome, Advanced)
    assert outcome.metrics["pages"] == 12.0


async def test_declared_mime_lying_does_not_stop_a_real_pdf(run, write, stub_probe) -> None:
    stub_probe()
    att = write("msa.pdf", b"%PDF-1.7")
    att.declared_mime = "application/octet-stream"
    outcome = await run(att)
    assert isinstance(outcome, Advanced)
    assert att.detected_mime == "application/pdf"
