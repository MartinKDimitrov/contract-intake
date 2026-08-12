"""Real mail is not ASCII, and a mangled name here poisons vendor matching later."""

from __future__ import annotations

from datetime import UTC, datetime

from contract_intake.adapters.models import (
    InboundAttachment,
    decode_mime_header,
    parse_date,
    parse_sender,
)


def test_decodes_cyrillic_base64_header() -> None:
    # Exactly what Gmail returned from the live mailbox.
    assert decode_mime_header("=?UTF-8?B?0YLQtdGB0YI=?=") == "тест"
    assert decode_mime_header("=?UTF-8?B?0JzQsNGA0YLQuNC9INCU0LjQvNC40YLRgNC+0LI=?=") == (
        "Мартин Димитров"
    )


def test_decodes_quoted_printable_header() -> None:
    assert decode_mime_header("=?utf-8?Q?Nordwind_Logistik_GmbH?=") == "Nordwind Logistik GmbH"


def test_plain_header_passes_through() -> None:
    assert decode_mime_header("Signed MSA") == "Signed MSA"


def test_missing_header_is_empty_not_none() -> None:
    assert decode_mime_header(None) == ""


def test_broken_encoding_degrades_instead_of_raising() -> None:
    """A malformed subject is not a reason to drop a contract."""
    raw = "=?UNKNOWN-CHARSET?B?bm90IHJlYWxseQ==?="
    assert decode_mime_header(raw)  # something came back; nothing blew up


def test_sender_is_extracted_and_normalised() -> None:
    assert parse_sender("Nordwind AP <AP@Nordwind.example>") == "ap@nordwind.example"
    assert parse_sender("bare@example.com") == "bare@example.com"
    assert parse_sender(None) == ""


def test_sender_with_encoded_display_name() -> None:
    header = "=?UTF-8?B?0JzQsNGA0YLQuNC9?= <mdimitrov@example.com>"
    assert parse_sender(header) == "mdimitrov@example.com"


def test_date_parsing_falls_back_when_absent_or_broken() -> None:
    fallback = datetime(2026, 8, 12, tzinfo=UTC)
    assert parse_date(None, fallback=fallback) == fallback
    assert parse_date("not a date", fallback=fallback) == fallback

    parsed = parse_date("Wed, 12 Aug 2026 19:57:01 +0300", fallback=fallback)
    assert parsed.year == 2026 and parsed.hour == 19


def test_attachment_digest_and_size() -> None:
    a = InboundAttachment(filename="msa.pdf", content=b"hello", declared_mime="application/pdf")
    b = InboundAttachment(filename="other.pdf", content=b"hello", declared_mime="application/pdf")
    assert a.size_bytes == 5
    assert a.sha256 == b.sha256, "the digest is content-based, so a rename is still a duplicate"
