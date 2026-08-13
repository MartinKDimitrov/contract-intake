"""What a failing stage is allowed to leave behind.

The pipeline's resumability rests on one claim: a stage either completes and
advances, or leaves nothing. These tests pin the two ways that claim was false
-- a failure whose half-written output stayed committed, and a failure the
book-keeping itself could not record.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from contract_intake.db.models import (
    Attachment,
    DeadLetter,
    Document,
    Enrichment,
    Extraction,
)
from contract_intake.pipeline import runner
from contract_intake.pipeline.base import Advanced, Failed, StageContext, StageOutcome
from contract_intake.status import Status


class WritesThenFails:
    """A stage that produces its output row and then does not finish."""

    number = 5
    name = "enrich"
    consumes = Status.EXTRACTED
    produces = Status.ENRICHED
    uses_llm = True

    def __init__(self, *, raise_it: bool = True) -> None:
        self.raise_it = raise_it

    async def run(self, ctx: StageContext) -> StageOutcome:
        document = Document(attachment_id=ctx.attachment_id, page_count=1)
        ctx.session.add(document)
        ctx.session.flush()
        extraction = Extraction(
            document_id=document.id, fields={}, model="claude-opus-5", effort="medium"
        )
        ctx.session.add(extraction)
        ctx.session.flush()
        ctx.session.add(Enrichment(extraction_id=extraction.id, findings=[]))
        ctx.session.flush()

        if self.raise_it:
            raise TimeoutError("the provider went away")
        return Failed(error=RuntimeError("declined"), retryable=True)


@pytest.fixture
def ready(session, attachment):
    attachment.status = Status.EXTRACTED
    session.commit()
    return attachment


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


@pytest.mark.parametrize("raise_it", [True, False], ids=["stage raises", "stage returns Failed"])
async def test_a_failed_stage_leaves_no_output_behind(session, settings, ready, raise_it) -> None:
    """Three attempts used to mean three rows and, for stage 05, three paid runs.

    Downstream stages all read `order_by(id.desc())`, so the orphans never
    surfaced as an error -- they surfaced as a bill.
    """
    stage = WritesThenFails(raise_it=raise_it)

    for _ in range(3):
        outcome = await runner.advance(ready, session=session, settings=settings, stage=stage)
        session.commit()
        assert isinstance(outcome, Failed)

    assert count(session, Enrichment) == 0
    assert count(session, Extraction) == 0


async def test_the_attempt_counter_survives_the_failure(session, settings, ready) -> None:
    """MAX_ATTEMPTS can only engage if the counter that drives it is recorded."""
    stage = WritesThenFails()

    await runner.advance(ready, session=session, settings=settings, stage=stage)
    session.commit()

    refreshed = session.get(Attachment, ready.id)
    assert refreshed is not None
    assert refreshed.attempts == 1
    assert "TimeoutError" in (refreshed.status_reason or ""), "the reason must not go stale"


async def test_a_bug_is_not_retried_three_times(session, settings, ready) -> None:
    """An AttributeError will fail the same way on every attempt.

    Retrying one burned three attempts and two and a half minutes of backoff --
    and, for the stages that call a model, three paid calls.
    """

    class Buggy(WritesThenFails):
        async def run(self, ctx: StageContext) -> StageOutcome:
            raise AttributeError("'NoneType' object has no attribute 'value'")

    outcome = await runner.advance(ready, session=session, settings=settings, stage=Buggy())
    session.commit()

    assert isinstance(outcome, Failed)
    assert not outcome.retryable
    refreshed = session.get(Attachment, ready.id)
    assert refreshed is not None
    assert refreshed.status == Status.DEAD
    assert count(session, DeadLetter) == 1


async def test_a_successful_stage_keeps_its_output(session, settings, ready) -> None:
    """The control: the savepoint must not discard work that succeeded."""

    class Succeeds(WritesThenFails):
        async def run(self, ctx: StageContext) -> StageOutcome:
            document = Document(attachment_id=ctx.attachment_id, page_count=1)
            ctx.session.add(document)
            ctx.session.flush()
            ctx.session.add(
                Extraction(
                    document_id=document.id,
                    fields={},
                    model="claude-opus-5",
                    effort="medium",
                )
            )
            ctx.session.flush()
            return Advanced(note="done")

    outcome = await runner.advance(ready, session=session, settings=settings, stage=Succeeds())
    session.commit()

    assert isinstance(outcome, Advanced)
    assert count(session, Extraction) == 1
    refreshed = session.get(Attachment, ready.id)
    assert refreshed is not None
    assert refreshed.status == Status.ENRICHED


async def test_a_database_level_failure_still_records_the_attempt(session, settings, ready) -> None:
    """The one failure class where the dead-letter guarantee used to be absent.

    When the stage's failure *is* a database failure, the session needs a
    rollback -- so the book-keeping written into that same session raised
    PendingRollbackError straight out of `drain`. The status never changed,
    `attempts` stayed 0, `MAX_ATTEMPTS` could never engage, and `pick_next`
    handed the same row back at full speed, forever.
    """

    class ViolatesAConstraint(WritesThenFails):
        async def run(self, ctx: StageContext) -> StageOutcome:
            ctx.session.add(Extraction(document_id=999_999, fields={}, model="m", effort="medium"))
            ctx.session.flush()  # foreign key failure poisons the session
            return Advanced(note="unreachable")

    await runner.advance(ready, session=session, settings=settings, stage=ViolatesAConstraint())

    session.rollback()
    session.expire_all()
    refreshed = session.get(Attachment, ready.id)
    assert refreshed is not None
    assert refreshed.attempts == 1, "the counter MAX_ATTEMPTS depends on must survive"
    assert count(session, Extraction) == 0
