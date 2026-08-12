from __future__ import annotations

from email.message import EmailMessage

import pytest

from contract_intake.adapters.imap import (
    CROWDED_FOLDER_THRESHOLD,
    ImapMailbox,
    UnsafeMailboxError,
    parse_message,
)


def build_mail(
    *,
    subject: str = "Signed MSA",
    sender: str = "AP <ap@nordwind.example>",
    attachments: tuple[tuple[str, bytes, str], ...] = (
        ("msa.pdf", b"%PDF-1.7 fake", "application/pdf"),
    ),
    inline_image: bool = False,
) -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = "<abc@example.com>"
    msg["From"] = sender
    msg["To"] = "contracts@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Wed, 12 Aug 2026 19:57:01 +0300"
    msg.set_content("Please find the signed agreement attached.")

    for filename, content, mime in attachments:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)

    if inline_image:
        msg.add_related(
            b"\x89PNG\r\n\x1a\n" + b"0" * 40, maintype="image", subtype="png", cid="<logo>"
        )

    return msg.as_bytes()


def test_parses_headers_and_attachments() -> None:
    parsed = parse_message(build_mail(), source="imap")
    assert parsed.message_id == "<abc@example.com>"
    assert parsed.sender == "ap@nordwind.example"
    assert parsed.subject == "Signed MSA"
    assert parsed.source == "imap"
    assert [a.filename for a in parsed.attachments] == ["msa.pdf"]
    assert parsed.attachments[0].content.startswith(b"%PDF")


def test_body_text_is_not_treated_as_an_attachment() -> None:
    parsed = parse_message(build_mail(attachments=()), source="imap")
    assert parsed.attachments == []


def test_multiple_attachments_are_all_returned() -> None:
    parsed = parse_message(
        build_mail(
            attachments=(
                ("msa.pdf", b"%PDF-1.7 a", "application/pdf"),
                ("annex.pdf", b"%PDF-1.7 b", "application/pdf"),
            )
        ),
        source="imap",
    )
    assert len(parsed.attachments) == 2
    assert parsed.attachments[0].sha256 != parsed.attachments[1].sha256


def test_cyrillic_subject_survives_the_round_trip() -> None:
    parsed = parse_message(build_mail(subject="тест договор"), source="imap")
    assert parsed.subject == "тест договор"


def test_message_without_id_still_parses() -> None:
    raw = build_mail().replace(b"Message-ID: <abc@example.com>\n", b"")
    parsed = parse_message(raw, source="imap")
    assert parsed.message_id, "a missing Message-ID must not produce an empty key"


# -- the personal-mailbox guard --------------------------------------------


def test_guard_blocks_a_crowded_inbox(settings) -> None:
    settings = settings.model_copy(update={"imap_folder": "INBOX"})
    mailbox = ImapMailbox(settings)
    with pytest.raises(UnsafeMailboxError) as exc:
        mailbox._guard_against_personal_mailbox("INBOX", CROWDED_FOLDER_THRESHOLD + 1)
    assert "personal mailbox" in str(exc.value)


def test_guard_allows_a_dedicated_label_of_any_size(settings) -> None:
    mailbox = ImapMailbox(settings.model_copy(update={"imap_folder": "contract-intake"}))
    mailbox._guard_against_personal_mailbox("contract-intake", 50_000)  # must not raise


def test_guard_permits_a_small_inbox_with_a_warning(settings, caplog) -> None:
    mailbox = ImapMailbox(settings.model_copy(update={"imap_folder": "INBOX"}))
    mailbox._guard_against_personal_mailbox("INBOX", 3)
    assert any("dedicated label is safer" in r.message for r in caplog.records)
