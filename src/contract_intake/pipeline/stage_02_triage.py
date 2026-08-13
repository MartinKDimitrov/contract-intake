"""Stage 02 -- Triage.

WHAT     Decide whether this file is worth spending a model call on.
IN       Status.RECEIVED
OUT      Status.TRIAGED, or Status.REJECTED for anything that is not a contract.
TOKENS   0. Heuristics only.
FAILS    zero-byte file, truncated/corrupt PDF, password-protected PDF,
         declared MIME lying about content, absurd page count, oversized file.
DEPENDS  loaders/detect.py, loaders/pdf.py

Why no model here: the cheapest token is the one never sent. Magic bytes, size,
encryption, page count and a vocabulary scan of page one settle the vast
majority of inputs for free. An invoice mailed to the contracts address costs
nothing to turn away.

Rejection is an expected outcome, not an incident -- no retry, no dead letter.
The reason is recorded so the sender can be told why.

Images are the deliberate soft spot: a photo of a signed page has no text to
scan, so it passes on size alone and stage 04 decides. Rejecting them here would
throw away exactly the messy real-world case worth handling.
"""

from __future__ import annotations

import logging
import unicodedata
from pathlib import Path
from typing import ClassVar

from contract_intake.db.models import Attachment
from contract_intake.loaders.detect import IMAGE_KINDS, MIME_BY_KIND, FileKind, sniff_path
from contract_intake.loaders.pdf import MAX_REASONABLE_PAGES, PdfProbe, probe
from contract_intake.pipeline.base import Advanced, Rejected, StageContext, StageOutcome
from contract_intake.status import Status

log = logging.getLogger(__name__)

ACCEPTED_KINDS = frozenset({FileKind.PDF}) | IMAGE_KINDS

#: Below this an image is a logo or an email signature, not a document page.
MIN_IMAGE_BYTES = 50_000

#: How much of the first line counts as the title, for disqualifying terms.
TITLE_CHARS = 120

#: Words that mark a legal instrument. A document without one of these is not a
#: contract, whatever else it says.
STRONG_TERMS: tuple[str, ...] = (
    # English
    "agreement",
    "whereas",
    "governing law",
    "in witness whereof",
    "counterparts",
    "shall be governed",
    "hereby agree",
    "is entered into",
    "the parties agree",
    "both parties agree",
    # Bulgarian
    "настоящия договор",
    "настоящият договор",
    "сключиха настоящия",
    "се сключи настоящия",
    "страните се споразумяха",
    "страните се договориха",
    "страните уговарят",
    "приложимо право",
    "приложимото право",
    # German
    "dieser vertrag",
    "der vorliegende vertrag",
    "vorliegender vertrag",
    "vereinbaren die parteien",
    "die parteien vereinbaren",
    "wird geschlossen zwischen",
    "anwendbares recht",
    "zwischen den parteien",
    # Spanish
    "el presente contrato",
    "las partes acuerdan",
    "ambas partes convienen",
    "ley aplicable",
    # French
    "le présent contrat",
    "la présente convention",
    "les parties conviennent",
    "droit applicable",
    "il a été convenu",
)

#: Commercial vocabulary that supports a classification but cannot carry it. An
#: acceptance protocol names a supplier and a vendor too.
SUPPORTING_TERMS: tuple[str, ...] = (
    # "hereby" belongs here, not above: certificates, declarations and
    # affidavits use it just as readily as contracts do.
    "hereby",
    "the parties",
    "both parties",
    "effective date",
    "termination",
    "indemnif",
    "confidential",
    "supplier",
    "vendor",
    "services",
    "clause",
    "obligations",
    "warrant",
    "liability",
    "party",
    "contract",
    "договор",
    "споразумение",
    "страните",
    "доставчик",
    "услуги",
    "клауза",
    "задължения",
    "прекратяване",
    "vertrag",
    "vereinbarung",
    "parteien",
    "kündigung",
    "lieferant",
    "leistungen",
    "haftung",
    "contrato",
    "acuerdo",
    "las partes",
    "proveedor",
    "servicios",
    "cláusula",
    "responsabilidad",
    "rescisión",
    # "hereinafter": defines a defined term, and a certificate defines those too
    "en adelante",
    "contrat",
    "convention",
    "les parties",
    "prestataire",
    "fournisseur",
    "prestations",
    "responsabilité",
    "résiliation",
    "ci-après",
)

INVOICE_TERMS: tuple[str, ...] = (
    # English
    # No entry may contain another: "invoice no" and "invoice number" both
    # contain "invoice", so one sentence in a supply agreement scored two
    # invoice hits and the document was rejected as an invoice.
    "invoice",
    "amount due",
    "subtotal",
    "bill to",
    "vat",
    "payment due",
    "remit to",
    "order total",
    "credit note",
    "quotation",
    "quote ref",
    "purchase order",
    "po number",
    # Bulgarian
    "фактура",
    "данъчна основа",
    "ддс",
    "сума за плащане",
    "ед. цена",
    "получател",
    "кредитно известие",
    "оферта",
    # Spanish
    "factura n",
    "base imponible",
    "importe total",
    "nota de crédito",
    "total a pagar",
    # French
    "facture n",
    "montant total",
    "net à payer",
    "bon de commande",
    "avoir n",
    # German
    "rechnung",
    "mwst",
    "gesamtbetrag",
    "nettobetrag",
    "zahlbar bis",
    "gutschrift",
    "bestellnummer",
)

#: Document types that are not contracts however they are worded. A certificate
#: attests, a declaration asserts, a resolution records -- none of them create
#: obligations between parties, and each says so in its own title.
#:
#: Matched against the opening of the document only. A real contract may well
#: require a certificate of insurance in clause 7, and that must not disqualify
#: it; a document that *is* a certificate announces the fact at the top.
DISQUALIFYING_TERMS: tuple[str, ...] = (
    "certificate of",
    "certificate no",
    "certificate registration",
    "certificate of registration",
    "hereby certifies",
    "certification body",
    "declaration of",
    "hereby declare",
    "we declare",
    "board resolution",
    "board decision",
    "the board of directors resolves",
    "audit report",
    "audit summary",
    "audit reference",
    "inspection report",
    "maintenance report",
    "inspection and maintenance",
    "acceptance protocol",
    "policy period",
    "сертификат",
    "декларация",
    "протокол",
    "решение на съвета",
    # Procurement notices: an announcement about a contract, not a contract.
    "contract notice",
    "award notice",
    "prior information notice",
    "обявление за поръчка",
    "обявление за възложена",
    "състезателна процедура",
    # English document types that read exactly like a contract and are not one
    "invitation to tender",
    "request for proposal",
    "instructions to tenderers",
    "terms of reference",
    "statement of work",
    "privacy policy",
    "employee handbook",
    "minutes of",
    # German
    "zertifikat",
    "bescheinigung",
    "hiermit wird bescheinigt",
    "konformitätserklärung",
    "erklärung",
    "abnahmeprotokoll",
    "protokoll",
    "prüfbericht",
    "beschluss des vorstands",
    "certificado de",
    "certifica que",
    "declaración",
    "declara que",
    "acta de recepción",
    "attestation de",
    "certifie que",
    "déclaration",
    "déclare que",
    "procès-verbal",
    "anuncio de licitación",
    "anuncio de adjudicación",
    "pliego de condiciones",
    "avis de marché",
    "avis d'attribution",
    "cahier des charges",
    "auftragsbekanntmachung",
    "bekanntmachung",
    "vergabebekanntmachung",
)

#: A contract needs one instrument marker plus something else -- a lone
#: "agreement" in a certificate's scope description is not enough.
MIN_STRONG_HITS = 1
MIN_TOTAL_HITS = 2


class TriageStage:
    number: ClassVar[int] = 2
    name: ClassVar[str] = "triage"
    consumes: ClassVar[Status] = Status.RECEIVED
    produces: ClassVar[Status] = Status.TRIAGED
    uses_llm: ClassVar[bool] = False

    async def run(self, ctx: StageContext) -> StageOutcome:
        attachment = ctx.session.get(Attachment, ctx.attachment_id)
        if attachment is None:
            return Rejected(reason=f"attachment {ctx.attachment_id} disappeared")

        path = Path(attachment.stored_path)
        if not path.exists():
            return Rejected(reason=f"stored file missing: {path}")

        if attachment.size_bytes == 0:
            return Rejected(reason="empty file")

        ceiling = ctx.settings.max_attachment_mb * 1024 * 1024
        if attachment.size_bytes > ceiling:
            return Rejected(
                reason=f"file is {attachment.size_bytes / 1e6:.1f} MB, "
                f"ceiling is {ctx.settings.max_attachment_mb} MB"
            )

        kind = sniff_path(path)
        attachment.detected_mime = MIME_BY_KIND[kind]
        if kind.value not in {k.value for k in ACCEPTED_KINDS}:
            return Rejected(reason=f"unsupported file type: {kind}")

        if attachment.declared_mime and attachment.declared_mime != attachment.detected_mime:
            log.info(
                "attachment %d declared %s but is %s; trusting the content",
                attachment.id,
                attachment.declared_mime,
                attachment.detected_mime,
            )

        if kind in IMAGE_KINDS:
            return _triage_image(attachment)
        return _triage_pdf(path)


def _triage_image(attachment: Attachment) -> StageOutcome:
    if attachment.size_bytes < MIN_IMAGE_BYTES:
        return Rejected(
            reason=f"image is only {attachment.size_bytes / 1024:.0f} KB; "
            "too small to be a document page"
        )
    return Advanced(
        note="image accepted without content check; stage 04 reads it",
        metrics={"pages": 1.0},
    )


def _triage_pdf(path: Path) -> StageOutcome:
    result = probe(path)

    if result.encrypted:
        return Rejected(reason="PDF is password-protected")
    if not result.readable:
        return Rejected(reason=f"PDF is unreadable: {result.error}")
    if result.page_count == 0:
        return Rejected(reason="PDF has no pages")
    if result.page_count > MAX_REASONABLE_PAGES:
        return Rejected(
            reason=f"{result.page_count} pages exceeds the {MAX_REASONABLE_PAGES}-page "
            "ceiling; likely a bundle rather than one contract"
        )

    # Before classifying: a page with no text layer has no vocabulary to judge,
    # and the fragment pdfplumber scrapes off a scan is not evidence of
    # anything. Classifying first let an invoice-shaped fragment veto a document
    # the code had already decided it could not read.
    if not result.has_text_layer:
        return Advanced(
            note="no text layer on page 1; stage 04 will read it as an image",
            metrics={"pages": float(result.page_count)},
        )

    verdict = classify_text(result.first_page_text)
    if verdict.kind == "invoice":
        return Rejected(reason=f"looks like an invoice, not a contract ({verdict.evidence})")
    if verdict.kind != "contract":
        return Rejected(reason=f"no contract vocabulary on page 1 ({verdict.evidence})")

    return Advanced(
        note=f"contract vocabulary found ({verdict.evidence})",
        metrics={"pages": float(result.page_count)},
    )


class TextVerdict:
    __slots__ = ("evidence", "kind")

    def __init__(self, kind: str, evidence: str) -> None:
        self.kind = kind
        self.evidence = evidence


def _folded(terms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(fold(t) for t in terms)


def fold(text: str) -> str:
    """Casefold, strip accents, and normalise the punctuation PDFs vary on.

    Accents are the point. ``casefold`` alone leaves "licitación" and
    "licitacion" as different strings, and a scanned or badly-encoded page
    routinely carries the second -- so an unaccented French contract was turned
    away while an unaccented Spanish tender notice was bought. The typographic
    apostrophe matters for the same reason: the real corpus carries four of them
    for every ASCII one, and "avis d'attribution" written with U+2019 matched
    nothing.
    """
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return folded.replace("\u2019", "'").replace("\u2018", "'").replace("\u00b4", "'")


#: The vocabulary, folded once at import on the same basis as the text it is
#: matched against. Fold one side only and every accented term stops matching.
_FOLDED_STRONG = _folded(STRONG_TERMS)
_FOLDED_SUPPORTING = _folded(SUPPORTING_TERMS)
_FOLDED_INVOICE = _folded(INVOICE_TERMS)
_FOLDED_DISQUALIFYING = _folded(DISQUALIFYING_TERMS)


def _title(text: str) -> str:
    """The first non-empty line -- where a document announces what it is.

    Disqualifiers used to be matched against the opening 320 characters, which
    is a paragraph rather than a title. A contract whose recitals name an
    annexed certificate of insurance was therefore turned away as a certificate,
    and the note on DISQUALIFYING_TERMS anticipated exactly that case
    without preventing it.
    """
    for line in text.splitlines():
        if line.strip():
            return fold(line)[:TITLE_CHARS]
    return ""


def classify_text(text: str) -> TextVerdict:
    """Cheap vocabulary check on the first page.

    Deliberately blunt. Its job is to turn away obvious non-contracts for free,
    not to be right about hard cases -- those go on to the model, which is what
    it is for.
    """
    lowered = fold(text)

    disqualifying = [t for t in _FOLDED_DISQUALIFYING if t in _title(text)]
    if disqualifying:
        return TextVerdict(
            "unknown", f"declares itself a {disqualifying[0]!r} document, not an agreement"
        )

    strong = [t for t in _FOLDED_STRONG if t in lowered]
    supporting = [t for t in _FOLDED_SUPPORTING if t in lowered]
    invoice = [t for t in _FOLDED_INVOICE if t in lowered]

    # An instrument marker outranks any number of invoice words. Every contract
    # has a payment clause, and this branch rejects outright rather than sending
    # to review, so letting it fire alongside a marker made it a silent-drop
    # machine aimed at exactly the documents we want.
    if not strong and len(invoice) >= 2:
        return TextVerdict("invoice", f"invoice terms: {', '.join(invoice[:3])}")

    if len(strong) >= MIN_STRONG_HITS and len(strong) + len(supporting) >= MIN_TOTAL_HITS:
        return TextVerdict("contract", f"terms: {', '.join((strong + supporting)[:3])}")

    return TextVerdict(
        "unknown",
        f"{len(strong)} instrument marker(s), {len(supporting)} supporting, "
        f"{len(invoice)} invoice term(s)",
    )


__all__ = ["PdfProbe", "TriageStage", "classify_text", "fold"]
