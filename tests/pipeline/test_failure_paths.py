"""What happens when things go wrong.

Most of a document-intake system's behaviour is failure behaviour: bad files,
flaky dependencies, a model that refuses, a process killed mid-run. These tests
cover the paths that only run on a bad day, which is exactly when nobody is
watching.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy import select

from contract_intake.db.models import Attachment, DeadLetter
from contract_intake.llm.client import BudgetExceededError, RefusalError, TruncatedError
from contract_intake.pipeline import runner
from contract_intake.pipeline.base import Advanced, Failed, Rejected, StageContext, StageOutcome
from contract_intake.pipeline.runner import MAX_ATTEMPTS, advance, pick_next
from contract_intake.status import Status


class _Stage:
    number: ClassVar[int] = 2
    name: ClassVar[str] = "triage"
    consumes: ClassVar[Status] = Status.RECEIVED
    produces: ClassVar[Status] = Status.TRIAGED
    uses_llm: ClassVar[bool] = False

    def __init__(self, outcome: StageOutcome | Exception) -> None:
        self._outcome = outcome

    async def run(self, ctx: StageContext) -> StageOutcome:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


@pytest.fixture
def swap(monkeypatch):
    return lambda stage: monkeypatch.setitem(runner.STAGE_BY_CONSUMES, Status.RECEIVED, stage)


# -- backoff ----------------------------------------------------------------


async def test_a_retryable_failure_is_not_retried_immediately(
    session, settings, attachment, swap
) -> None:
    """A rate limit answered with another request straight away is not a retry."""
    swap(_Stage(Failed(error=RuntimeError("429"), retryable=True)))
    await advance(attachment, session=session, settings=settings)

    assert attachment.retry_after is not None
    assert attachment.retry_after > datetime.now(UTC)
    assert pick_next(session) is None, "the row must not be runnable yet"


async def test_the_wait_grows_with_each_attempt(session, settings, attachment, swap) -> None:
    swap(_Stage(Failed(error=RuntimeError("flaky"), retryable=True)))
    waits: list[timedelta] = []

    for _ in range(MAX_ATTEMPTS - 1):
        await advance(attachment, session=session, settings=settings)
        assert attachment.retry_after is not None
        waits.append(attachment.retry_after - datetime.now(UTC))
        attachment.retry_after = None  # let the next attempt through
        session.flush()

    assert waits[1] > waits[0], "a second failure should wait longer than the first"


async def test_work_becomes_runnable_once_the_wait_passes(
    session, settings, attachment, swap
) -> None:
    swap(_Stage(Failed(error=RuntimeError("timeout"), retryable=True)))
    await advance(attachment, session=session, settings=settings)
    assert pick_next(session) is None

    attachment.retry_after = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    assert pick_next(session) is not None


async def test_success_clears_the_backoff(session, settings, attachment, swap) -> None:
    swap(_Stage(Failed(error=RuntimeError("blip"), retryable=True)))
    await advance(attachment, session=session, settings=settings)

    swap(_Stage(Advanced()))
    attachment.retry_after = None
    session.flush()
    await advance(attachment, session=session, settings=settings)

    assert attachment.retry_after is None
    assert attachment.attempts == 0


# -- what is retryable and what is not --------------------------------------


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (TruncatedError("hit max_tokens"), True),
        (BudgetExceededError("ceiling reached"), False),
        (RefusalError("cyber", "declined"), False),
    ],
)
async def test_errors_are_classified_rather_than_lumped_together(
    session, settings, attachment, swap, error: Exception, retryable: bool
) -> None:
    """Retrying a refusal wastes money; retrying a truncation is the fix."""
    swap(_Stage(Failed(error=error, retryable=retryable)))
    await advance(attachment, session=session, settings=settings)

    if retryable:
        assert attachment.status == Status.RECEIVED
    else:
        assert attachment.status == Status.DEAD


async def test_a_dead_letter_records_enough_to_act_on(session, settings, attachment, swap) -> None:
    swap(_Stage(Failed(error=ValueError("password-protected"), retryable=False)))
    await advance(attachment, session=session, settings=settings)

    letter = session.scalars(select(DeadLetter)).one()
    assert letter.stage == "triage"
    assert letter.error_class == "ValueError"
    assert "password-protected" in letter.message
    assert letter.attachment_id == attachment.id


async def test_an_unexpected_exception_does_not_take_the_worker_down(
    session, settings, attachment, swap
) -> None:
    swap(_Stage(MemoryError("rasterising a 400-page scan")))
    outcome = await advance(attachment, session=session, settings=settings)

    assert isinstance(outcome, Failed)
    assert attachment.attempts == 1
    assert attachment.status == Status.RECEIVED, "still retryable, not dead"


async def test_a_rejection_leaves_no_dead_letter(session, settings, attachment, swap) -> None:
    """An invoice sent to the contracts address is an outcome, not an incident."""
    swap(_Stage(Rejected(reason="looks like an invoice")))
    await advance(attachment, session=session, settings=settings)

    assert attachment.status == Status.REJECTED
    assert session.scalars(select(DeadLetter)).all() == []


# -- resumption -------------------------------------------------------------


async def test_a_document_resumes_at_the_phase_it_stopped_in(
    session, settings, attachment, swap
) -> None:
    """The whole argument for keeping status in the database rather than memory."""
    swap(_Stage(Advanced()))
    await advance(attachment, session=session, settings=settings)
    assert attachment.status == Status.TRIAGED

    # A restart loses everything except the row, which is enough.
    session.expire_all()
    reloaded = session.get(Attachment, attachment.id)
    assert reloaded is not None
    assert reloaded.status == Status.TRIAGED
    assert runner.STAGE_BY_CONSUMES[Status.TRIAGED].name == "load"


async def test_exhausted_work_is_not_picked_up_forever(session, attachment) -> None:
    attachment.attempts = MAX_ATTEMPTS
    session.flush()
    assert pick_next(session) is None


# -- bad input --------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("empty.pdf", b""),
        ("truncated.pdf", b"%PDF-1.7\n" + b"\xde\xad\xbe\xef" * 40),
        ("renamed.pdf", b"MZ\x90\x00" + b"\x00" * 300),
        ("archive.pdf", b"PK\x03\x04" + b"\x00" * 300),
        ("nul.pdf", b"\x00" * 500),
        ("html.pdf", b"<!doctype html><html><body>not a contract</body></html>"),
    ],
)
async def test_malformed_files_are_rejected_rather_than_crashing(
    session, settings, attachment, name: str, content: bytes
) -> None:
    from contract_intake.pipeline.stage_02_triage import TriageStage

    settings.ensure_dirs()
    path = settings.attachments_dir / name
    path.write_bytes(content)

    row = Attachment(
        email_id=attachment.email_id,
        filename=name,
        sha256=hashlib.sha256(content or name.encode()).hexdigest(),
        declared_mime="application/pdf",
        size_bytes=len(content),
        stored_path=str(path),
        status=Status.RECEIVED,
    )
    session.add(row)
    session.flush()

    outcome = await TriageStage().run(
        StageContext(attachment_id=row.id, session=session, settings=settings)
    )
    assert isinstance(outcome, Rejected), f"{name} should be turned away, not crash"
    assert outcome.reason


async def test_a_vanished_file_is_rejected_not_retried_forever(
    session, settings, attachment
) -> None:
    from contract_intake.pipeline.stage_02_triage import TriageStage

    attachment.stored_path = str(Path(settings.data_dir) / "gone.pdf")
    session.flush()
    outcome = await TriageStage().run(
        StageContext(attachment_id=attachment.id, session=session, settings=settings)
    )
    assert isinstance(outcome, Rejected)


async def test_a_stage_needing_an_llm_fails_loudly_without_one(
    session, settings, attachment
) -> None:
    """A missing client is a configuration error, and retrying will not fix it."""
    from contract_intake.pipeline.stage_04_extract import ExtractStage

    outcome = await ExtractStage().run(
        StageContext(attachment_id=attachment.id, session=session, settings=settings, llm=None)
    )
    assert isinstance(outcome, Failed)
    assert outcome.retryable is False
