"""Generate synthetic contract fixtures.

Everything the pipeline is tested and demonstrated against is generated here.
No real contract, company or counterparty appears anywhere in this repository --
the vendor registry, the playbook and these documents are all invented.

Two output shapes, matching the two real-world inputs that behave differently:

* ``text``  -- a born-digital PDF with a text layer. Cheap to read.
* ``scan``  -- the same content rasterised and degraded, with no text layer, so
  it has to go to the model as an image. This is the expensive path, and the
  one that exercises counterparty resolution against a noisy name.

Written with a minimal hand-rolled PDF writer rather than a reporting library:
the fixtures need to be reproducible byte-for-byte and the project should not
carry a document-generation dependency it uses nowhere else.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

FIXTURES = Path(__file__).parent


FONT = FIXTURES / "fonts" / "DejaVuSans.ttf"


def write_text_pdf(path: Path, pages: list[str], *, font_size: int = 10) -> Path:
    """Write a PDF with a real, extractable text layer.

    Uses an embedded Unicode font rather than one of the fourteen standard PDF
    fonts. Those are Latin-1 only: a Cyrillic document written with Helvetica
    round-trips into glyph codes, which meant the Bulgarian fixture was silently
    testing a corrupted file rather than Bulgarian.

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
        pdf.multi_cell(
            w=pdf.epw - 20,
            h=font_size * 0.52,
            text=body.strip("\n"),
            align="L",
        )

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


# -- the documents ----------------------------------------------------------

CLEAN = [
    """
MASTER SERVICES AGREEMENT

This Master Services Agreement (the "Agreement") is entered into as of
14 March 2026 (the "Effective Date") by and between:

  Meridian Rail Holdings AD, UIC 205118874, of 12 Tsarigradsko Shose,
  Sofia 1784, Bulgaria ("Customer"); and

  Nordwind Logistik GmbH, HRB 84421, of Hafenstrasse 19, 20359 Hamburg,
  Germany ("Supplier").

WHEREAS the Supplier provides freight forwarding and customs brokerage
services, and the Customer wishes to procure such services, the parties
hereby agree as follows.

1. TERM
   This Agreement commences on the Effective Date and continues for an
   initial term of twenty-four (24) months. It shall not renew
   automatically; renewal requires a written agreement of both parties.

2. CHARGES AND PAYMENT
   The Customer shall pay each undisputed invoice within forty-five (45)
   days of receipt.

3. LIMITATION OF LIABILITY
   The aggregate liability of either party under this Agreement shall not
   exceed EUR 500,000.
""",
    """
4. TERMINATION
   Either party may terminate this Agreement for convenience upon sixty
   (60) days written notice to the other party.

5. CONFIDENTIALITY
   Each party shall keep confidential all information disclosed by the
   other and marked or reasonably understood to be confidential.

6. DATA PROTECTION
   The parties have executed a Data Processing Agreement, attached as
   Annex B, which forms an integral part of this Agreement.

7. GOVERNING LAW
   This Agreement is governed by the laws of the Republic of Bulgaria.
   The courts of Sofia shall have exclusive jurisdiction.

IN WITNESS WHEREOF the parties have executed this Agreement.

  For Meridian Rail Holdings AD          For Nordwind Logistik GmbH
  ______________________                 ______________________
  I. Petrova, CFO                        K. Brandt, Managing Director
""",
]

DEVIATION = [
    """
SUPPLY AND SERVICES AGREEMENT

Dated 2 April 2026 between Meridian Rail Holdings AD ("Customer") and
Kestrel Analytics Ltd, company number 09912447, of 41 Fenchurch Street,
London EC3M 4BS, United Kingdom ("Supplier").

WHEREAS the Supplier shall provide data analytics services, the parties
hereby agree as follows.

1. TERM AND RENEWAL
   The initial term is twelve (12) months from the date above. This
   Agreement shall renew automatically for successive twelve (12) month
   periods unless either party gives notice not less than ninety (90)
   days prior to the end of the then-current term.

2. PAYMENT TERMS
   The Customer shall settle all invoices within ninety (90) days of the
   invoice date.

3. LIABILITY
   Save for death or personal injury, neither party shall be liable to
   the other for any loss whatsoever.

4. GOVERNING LAW
   This Agreement shall be governed by the laws of the Cayman Islands.

Signed for and on behalf of the parties.
""",
]

UNKNOWN_VENDOR = [
    """
SERVICES AGREEMENT

This Agreement is made on 21 May 2026 between Meridian Rail Holdings AD
("Customer") and NordWind Logistics Ltd. ("Supplier"), of Hamburg.

WHEREAS the Supplier provides logistics services, the parties agree:

1. TERM. Eighteen (18) months from the date of this Agreement, with no
   automatic renewal.

2. PAYMENT. Invoices are payable within forty-five (45) days.

3. LIABILITY CAP. Neither party's aggregate liability shall exceed
   EUR 250,000.

4. TERMINATION. Ninety (90) days written notice by either party.

5. GOVERNING LAW. The laws of Germany shall apply.

Executed by the duly authorised representatives of the parties.
""",
]

INVOICE = [
    """
INVOICE

Invoice No: 2026-00841              Date: 3 June 2026
Bill to: Meridian Rail Holdings AD

Description                     Qty        Unit        Amount
Freight forwarding, May 2026      1     8,400.00      8,400.00
Customs brokerage                 3       220.00        660.00

                                       Subtotal       9,060.00
                                       VAT 20%        1,812.00
                                       Amount due    10,872.00

Payment due within 30 days. Remit to IBAN DE00 2005 0000 0000 0000 00.
""",
]

DOCUMENTS = {
    "01-clean-known-vendor": (CLEAN, "text"),
    "02-scan-fuzzy-vendor": (UNKNOWN_VENDOR, "scan"),
    "03-policy-deviations": (DEVIATION, "text"),
    "04-not-a-contract": (INVOICE, "text"),
}


SOURCE = FIXTURES / "source"


def render_source_documents(out: Path) -> list[str]:
    """Render the plain-text corpus in `source/` into PDFs.

    Those documents are the varied half of the corpus -- amendments, a bilingual
    lease, an invoice in Cyrillic, certificates and quotes that are not contracts
    at all. They are kept as text so the wording is reviewable in a diff, and
    rendered here so the pipeline sees the same kind of file it sees in the wild.
    """
    if not SOURCE.exists():
        return []
    made: list[str] = []
    for path in sorted(SOURCE.glob("*.txt")):
        body = path.read_text(encoding="utf-8")
        pages = [body[i : i + 2600] for i in range(0, len(body), 2600)] or [body]
        write_text_pdf(out / f"{path.stem}.pdf", pages, font_size=9)
        made.append(path.stem)
    return made


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=FIXTURES)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    for name, (pages, shape) in DOCUMENTS.items():
        path = args.out / f"{name}.pdf"
        if shape == "scan":
            write_scan(path, pages)
        else:
            write_text_pdf(path, pages)
        print(f"{path.name:<28} {shape:<5} {path.stat().st_size / 1024:6.1f} KB")

    for name in render_source_documents(args.out):
        size = (args.out / f"{name}.pdf").stat().st_size / 1024
        print(f"{name + '.pdf':<28} {'text':<5} {size:6.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
