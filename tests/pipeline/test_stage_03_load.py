"""The per-page text-versus-vision decision -- the largest cost lever there is.

Runs against the generated fixtures rather than mocks: the thing being tested is
whether a real PDF is read the way we think, and a mock cannot answer that.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select

from contract_intake.db.models import Attachment
from contract_intake.db.models import Document as DocumentRow
from contract_intake.loaders.document import Document, load, page_content_blocks
from contract_intake.pipeline.base import Advanced, Rejected, StageContext
from contract_intake.pipeline.stage_03_load import LoadStage
from contract_intake.status import Status

FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures"
BORN_DIGITAL = FIXTURES / "01-clean-known-vendor.pdf"
SCANNED = FIXTURES / "02-scan-fuzzy-vendor.pdf"

pytestmark = pytest.mark.skipif(
    not BORN_DIGITAL.exists(),
    reason="fixtures not generated; run evals/fixtures/generate.py",
)


@pytest.fixture
def run(session, settings, attachment):
    async def _run(source: Path):
        row = Attachment(
            email_id=attachment.email_id,
            filename=source.name,
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            declared_mime="application/pdf",
            detected_mime="application/pdf",
            size_bytes=source.stat().st_size,
            stored_path=str(source),
            status=Status.TRIAGED,
        )
        session.add(row)
        session.flush()
        ctx = StageContext(attachment_id=row.id, session=session, settings=settings)
        return await LoadStage().run(ctx), row

    return _run


async def test_born_digital_pages_stay_as_text(run, session) -> None:
    outcome, row = await run(BORN_DIGITAL)
    assert isinstance(outcome, Advanced)

    stored = session.scalars(select(DocumentRow).where(DocumentRow.attachment_id == row.id)).one()
    assert stored.text_pages == 2
    assert stored.image_pages == 0, "a text layer must never be rasterised"


async def test_scan_without_text_layer_is_rendered(run, settings) -> None:
    outcome, _ = await run(SCANNED)
    assert isinstance(outcome, Advanced)
    assert outcome.metrics["image_pages"] == 1.0
    assert outcome.metrics["text_pages"] == 0.0


async def test_rendered_page_respects_the_resolution_ceiling(settings, tmp_path) -> None:
    document = load(SCANNED, settings=settings, into=tmp_path)
    page = document.pages[0]
    assert max(page.width, page.height) <= settings.page_image_max_px
    assert Path(page.image_path).exists()


async def test_an_image_page_costs_several_times_a_text_page(settings, tmp_path) -> None:
    """The whole reason this decision is made per page rather than per document."""
    text_doc = load(BORN_DIGITAL, settings=settings, into=tmp_path / "a")
    scan_doc = load(SCANNED, settings=settings, into=tmp_path / "b")

    per_text_page = text_doc.estimated_tokens / text_doc.page_count
    per_image_page = scan_doc.estimated_tokens / scan_doc.page_count
    assert per_image_page > per_text_page * 4


async def test_lower_resolution_costs_proportionally_less(settings, tmp_path) -> None:
    cheap = settings.model_copy(update={"page_image_max_px": 700})
    big = load(SCANNED, settings=settings, into=tmp_path / "big")
    small = load(SCANNED, settings=cheap, into=tmp_path / "small")
    assert small.estimated_tokens < big.estimated_tokens / 2


async def test_missing_file_is_rejected(run, settings, session, attachment) -> None:
    row = Attachment(
        email_id=attachment.email_id,
        filename="gone.pdf",
        sha256="b" * 64,
        declared_mime="application/pdf",
        size_bytes=10,
        stored_path=str(settings.data_dir / "nope.pdf"),
        status=Status.TRIAGED,
    )
    session.add(row)
    session.flush()
    ctx = StageContext(attachment_id=row.id, session=session, settings=settings)
    outcome = await LoadStage().run(ctx)
    assert isinstance(outcome, Rejected)


# -- content blocks ---------------------------------------------------------


def test_text_pages_become_text_blocks_not_images(settings, tmp_path) -> None:
    blocks = page_content_blocks(load(BORN_DIGITAL, settings=settings, into=tmp_path))
    assert all(b["type"] == "text" for b in blocks)
    assert "page 1" in blocks[0]["text"]


def test_image_pages_become_image_blocks(settings, tmp_path) -> None:
    blocks = page_content_blocks(load(SCANNED, settings=settings, into=tmp_path))
    kinds = [b["type"] for b in blocks]
    assert "image" in kinds
    image = next(b for b in blocks if b["type"] == "image")
    assert image["source"]["media_type"] == "image/png"
    assert image["source"]["data"], "base64 payload must be populated"


def test_document_round_trips_through_json(settings, tmp_path) -> None:
    original = load(BORN_DIGITAL, settings=settings, into=tmp_path)
    restored = Document.from_json(original.to_json())
    assert restored.page_count == original.page_count
    assert restored.all_text == original.all_text
