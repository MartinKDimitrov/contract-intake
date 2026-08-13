"""Render the text documents under ``evals/documents/`` to PDF.

The pipeline takes PDFs; the corpus is kept as plain text so that the wording of
every document is reviewable in a diff rather than buried in a binary. This
script is the bridge, and it is the reason ``documents/rendered/`` is not
committed: it is derived, and derived files drift.

Two output shapes, matching the two real-world inputs that behave differently:

* ``text`` -- a born-digital PDF with a text layer. Cheap to read.
* ``scan`` -- the same content rasterised and degraded, with no text layer, so
  it has to go to the model as an image. This is the expensive path, and the one
  that exercises counterparty resolution against a noisy name. A source file
  named ``*.scan.txt`` takes this path.

Page breaks in the source are a form feed (``\\f``).
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

EVALS = Path(__file__).parent
DOCUMENTS = EVALS / "documents"
RENDERED = DOCUMENTS / "rendered"
FONT = EVALS / "fonts" / "DejaVuSans.ttf"

#: Provenance folders holding text sources. ``collected/`` is already PDF.
SOURCES = ("authored", "generated")


def write_text_pdf(path: Path, pages: list[str], *, font_size: int = 9) -> Path:
    """Write a PDF with a real, extractable text layer.

    Uses an embedded Unicode font rather than one of the fourteen standard PDF
    fonts. Those are Latin-1 only: a Cyrillic document written with Helvetica
    round-trips into glyph codes, which meant the Bulgarian document was
    silently testing a corrupted file rather than Bulgarian.

    Font embedding done properly needs a CIDFontType2 descendant, Identity-H
    encoding, subsetting and a ToUnicode CMap -- without that last one the text
    is drawn correctly and still extracts as glyph ids. That is not something to
    hand-roll, so fpdf2 does it, as a dev-only dependency.
    """
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_font("body", fname=str(FONT))

    for body in pages:
        pdf.add_page()
        pdf.set_font("body", size=font_size)
        pdf.set_xy(18, 18)
        pdf.multi_cell(w=pdf.epw - 20, h=font_size * 0.52, text=body.strip("\n"), align="L")

    pdf.output(str(path))
    return path


def write_scan(path: Path, pages: list[str], *, seed: int = 7) -> Path:
    """Rasterise the text and degrade it, producing a PDF with no text layer."""
    from PIL import Image, ImageDraw, ImageFilter

    rng = random.Random(seed)
    images = []
    for body in pages:
        canvas = Image.new("L", (1240, 1754), color=248)
        draw = ImageDraw.Draw(canvas)
        y = 90
        for line in body.strip("\n").split("\n"):
            draw.text((100, y), line, fill=40)
            y += 26

        canvas = canvas.rotate(rng.uniform(-1.2, 1.2), fillcolor=248, resample=Image.BICUBIC)
        canvas = canvas.filter(ImageFilter.GaussianBlur(0.6))
        speckle = Image.effect_noise(canvas.size, 14).convert("L")
        canvas = Image.blend(canvas, speckle, 0.10)
        images.append(canvas.convert("RGB"))

    images[0].save(path, save_all=True, append_images=images[1:], resolution=150.0)
    return path


def sources() -> list[Path]:
    found: list[Path] = []
    for folder in SOURCES:
        found.extend((DOCUMENTS / folder).rglob("*.txt"))
    return sorted(found, key=lambda p: p.name)


def render(out: Path) -> list[tuple[str, str]]:
    """Render every text source to ``out``. Returns (name, shape) pairs."""
    out.mkdir(parents=True, exist_ok=True)
    made: list[tuple[str, str]] = []

    for path in sources():
        body = path.read_text(encoding="utf-8")
        pages = body.split("\f") if "\f" in body else [body]
        scan = path.name.endswith(".scan.txt")
        name = path.stem.removesuffix(".scan")
        if scan:
            write_scan(out / f"{name}.pdf", pages)
        else:
            write_text_pdf(out / f"{name}.pdf", pages)
        made.append((name, "scan" if scan else "text"))

    return made


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=RENDERED)
    args = parser.parse_args()

    made = render(args.out)
    for name, shape in made:
        size = (args.out / f"{name}.pdf").stat().st_size / 1024
        print(f"{name + '.pdf':<34} {shape:<5} {size:6.1f} KB")
    print(f"\n{len(made)} document(s) in {args.out}")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(main())
