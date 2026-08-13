"""The loaded document, and the per-page decision that sets most of the bill.

A page reaches the model one of two ways:

* as **text**, when it has a usable text layer -- a few hundred tokens;
* as an **image**, when it does not -- a few thousand.

The decision is made per page, not per document, which is the whole point. A
born-digital contract with one scanned signature page sends nineteen pages of
text and exactly one image, instead of twenty images.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from contract_intake.config import Settings
from contract_intake.loaders.detect import IMAGE_KINDS, FileKind, sniff_path
from contract_intake.loaders.image import encode_page_image, normalise_image
from contract_intake.loaders.redact import redact

log = logging.getLogger(__name__)

PageKind = Literal["text", "image"]

#: Anthropic bills images at roughly (width * height) / 750 tokens.
IMAGE_TOKENS_PER_PIXEL = 1 / 750

#: A rough characters-per-token ratio for English contract prose, used only for
#: the estimate reported alongside each document. Real numbers come from the
#: ledger, not from here.
CHARS_PER_TOKEN = 3.7


@dataclass(frozen=True, slots=True)
class Page:
    number: int
    kind: PageKind
    text: str = ""
    image_path: str = ""
    width: int = 0
    height: int = 0

    @property
    def estimated_tokens(self) -> int:
        if self.kind == "text":
            return int(len(self.text) / CHARS_PER_TOKEN)
        return int(self.width * self.height * IMAGE_TOKENS_PER_PIXEL)

    def to_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "kind": self.kind,
            "text": self.text,
            "image_path": self.image_path,
            "width": self.width,
            "height": self.height,
            "estimated_tokens": self.estimated_tokens,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Page:
        return cls(
            number=int(raw["number"]),
            kind=raw["kind"],
            text=raw.get("text", ""),
            image_path=raw.get("image_path", ""),
            width=int(raw.get("width", 0)),
            height=int(raw.get("height", 0)),
        )


@dataclass(frozen=True, slots=True)
class Document:
    pages: list[Page]
    #: How many items of each personal-data category were masked, by category.
    #: Empty means the text was clean; the flag below distinguishes that from
    #: redaction having been switched off.
    redactions: dict[str, int] = field(default_factory=dict)
    redacted: bool = False

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def text_pages(self) -> int:
        return sum(1 for p in self.pages if p.kind == "text")

    @property
    def image_pages(self) -> int:
        return sum(1 for p in self.pages if p.kind == "image")

    @property
    def estimated_tokens(self) -> int:
        return sum(p.estimated_tokens for p in self.pages)

    @property
    def all_text(self) -> str:
        """Concatenated text layer, used to verify extraction quotes."""
        return "\n".join(p.text for p in self.pages if p.kind == "text")

    def mask_personal_data(self) -> Document:
        """Return a copy with personal data masked out of every text page.

        Image pages pass through untouched -- there is no text layer to search,
        so a scanned page reaches the model as photographed. That gap is real
        and is documented rather than papered over.
        """
        pages: list[Page] = []
        found: Counter[str] = Counter()
        for page in self.pages:
            if page.kind != "text" or not page.text:
                pages.append(page)
                continue
            text, counts = redact(page.text)
            found.update(counts)
            pages.append(replace(page, text=text))
        return Document(pages=pages, redactions=dict(found), redacted=True)

    def to_json(self) -> list[dict[str, Any]]:
        return [p.to_json() for p in self.pages]

    @classmethod
    def from_json(cls, raw: list[dict[str, Any]]) -> Document:
        return cls(pages=[Page.from_json(p) for p in raw])


def load(path: Path, *, settings: Settings, into: Path) -> Document:
    """Load a file into pages, rendering only what has no text layer.

    Personal data is masked here, at the point the text comes into existence,
    so that no later stage -- and no database row -- ever holds the raw value.
    """
    kind = sniff_path(path)
    if kind in IMAGE_KINDS:
        document = _load_image(path, settings=settings, into=into)
    elif kind is FileKind.PDF:
        document = _load_pdf(path, settings=settings, into=into)
    else:
        raise ValueError(f"cannot load {kind} as a document")

    return document.mask_personal_data() if settings.redact_personal_data else document


def _load_image(path: Path, *, settings: Settings, into: Path) -> Document:
    into.mkdir(parents=True, exist_ok=True)
    target = into / "p001.png"
    width, height = normalise_image(path, target, max_px=settings.page_image_max_px)
    return Document(
        pages=[Page(number=1, kind="image", image_path=str(target), width=width, height=height)]
    )


def _load_pdf(path: Path, *, settings: Settings, into: Path) -> Document:
    import pdfplumber

    into.mkdir(parents=True, exist_ok=True)
    pages: list[Page] = []

    with pdfplumber.open(str(path)) as pdf:
        needs_render: list[int] = []
        for index, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").strip()
            if len(text) >= settings.min_text_chars_per_page:
                pages.append(Page(number=index, kind="text", text=text))
            else:
                needs_render.append(index)
                pages.append(Page(number=index, kind="image"))

    if needs_render:
        rendered = _render_pages(path, needs_render, into=into, max_px=settings.page_image_max_px)
        pages = [rendered.get(p.number, p) for p in pages]

    log.info(
        "loaded %s: %d page(s), %d as text, %d as image",
        path.name,
        len(pages),
        sum(1 for p in pages if p.kind == "text"),
        sum(1 for p in pages if p.kind == "image"),
    )
    return Document(pages=pages)


def _render_pages(
    path: Path,
    numbers: list[int],
    *,
    into: Path,
    max_px: int,
) -> dict[int, Page]:
    """Rasterise only the pages that need it."""
    import pypdfium2

    out: dict[int, Page] = {}
    pdf = pypdfium2.PdfDocument(str(path))
    try:
        for number in numbers:
            source = pdf[number - 1]
            longest = max(source.get_width(), source.get_height()) or 1
            scale = min(max_px / longest, 4.0)
            bitmap = source.render(scale=scale)
            image = bitmap.to_pil().convert("RGB")

            target = into / f"p{number:03d}.png"
            image.save(target, format="PNG", optimize=True)
            out[number] = Page(
                number=number,
                kind="image",
                image_path=str(target),
                width=image.width,
                height=image.height,
            )
    finally:
        pdf.close()
    return out


def page_content_blocks(document: Document) -> list[dict[str, Any]]:
    """Render the document as Anthropic content blocks, cheapest form per page."""
    blocks: list[dict[str, Any]] = []
    for page in document.pages:
        if page.kind == "text":
            blocks.append({"type": "text", "text": f"--- page {page.number} ---\n{page.text}"})
            continue
        blocks.append({"type": "text", "text": f"--- page {page.number} (scanned) ---"})
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": encode_page_image(Path(page.image_path)),
                },
            }
        )
    return blocks
