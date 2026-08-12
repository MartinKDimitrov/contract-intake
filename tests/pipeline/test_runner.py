"""The stage chain is the backbone; these tests are what keep it honest.

Splitting phases across files buys discoverability at the cost of a new failure
mode: rename a status in one file, forget its neighbour, and a document advances
into a status nobody consumes -- stalling silently forever. validate_chain()
turns that into an import-time crash, and these tests keep it that way.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from sqlalchemy import select

from contract_intake.db.models import DeadLetter
from contract_intake.pipeline.base import (
    Advanced,
    Failed,
    Rejected,
    Stage,
    StageContext,
    StageOutcome,
)
from contract_intake.pipeline.runner import (
    MAX_ATTEMPTS,
    STAGE_BY_CONSUMES,
    STAGES,
    BrokenPipelineError,
    advance,
    pick_next,
    validate_chain,
)
from contract_intake.status import Status


def test_real_chain_is_contiguous() -> None:
    validate_chain()


def test_every_stage_number_is_unique_and_ordered() -> None:
    numbers = [s.number for s in STAGES]
    assert numbers == sorted(numbers) == list(range(2, 2 + len(STAGES)))


def test_every_non_terminal_status_has_exactly_one_consumer() -> None:
    consumed = [s.consumes for s in STAGES]
    assert len(consumed) == len(set(consumed)), "two stages claim the same status"


def test_chain_gap_is_caught() -> None:
    class Wrong:
        number: ClassVar[int] = 3
        name: ClassVar[str] = "wrong"
        consumes: ClassVar[Status] = Status.EXTRACTED  # gap: nothing produces this yet
        produces: ClassVar[Status] = Status.ENRICHED
        uses_llm: ClassVar[bool] = False

        async def run(self, ctx: StageContext) -> StageOutcome:
            return Advanced()

    with pytest.raises(BrokenPipelineError):
        validate_chain((STAGES[0], Wrong()))  # type: ignore[arg-type]


# -- driver behaviour -------------------------------------------------------


class _Fake:
    number: ClassVar[int] = 2
    name: ClassVar[str] = "fake"
    consumes: ClassVar[Status] = Status.RECEIVED
    produces: ClassVar[Status] = Status.TRIAGED
    uses_llm: ClassVar[bool] = False

    def __init__(self, outcome: StageOutcome) -> None:
        self._outcome = outcome

    async def run(self, ctx: StageContext) -> StageOutcome:
        return self._outcome


@pytest.fixture
def swap_stage(monkeypatch):
    def _swap(stage: Stage) -> None:
        monkeypatch.setitem(STAGE_BY_CONSUMES, Status.RECEIVED, stage)

    return _swap


async def test_pick_next_finds_runnable_work(session, attachment) -> None:
    assert pick_next(session) is not None
    attachment.status = Status.DELIVERED
    session.flush()
    assert pick_next(session) is None


async def test_advance_moves_status_forward(session, settings, attachment, swap_stage) -> None:
    swap_stage(_Fake(Advanced(note="looks like a contract")))
    await advance(attachment, session=session, settings=settings)
    assert attachment.status == Status.TRIAGED
    assert attachment.status_reason == "looks like a contract"
    assert attachment.attempts == 0


async def test_rejection_is_terminal_and_not_an_error(
    session, settings, attachment, swap_stage
) -> None:
    swap_stage(_Fake(Rejected(reason="not a contract: invoice keywords")))
    await advance(attachment, session=session, settings=settings)
    assert attachment.status == Status.REJECTED
    assert "invoice" in (attachment.status_reason or "")
    assert session.scalars(select(DeadLetter)).all() == []


async def test_retryable_failure_retries_then_dead_letters(
    session, settings, attachment, swap_stage
) -> None:
    swap_stage(_Fake(Failed(error=RuntimeError("timeout"), retryable=True)))

    for _ in range(MAX_ATTEMPTS - 1):
        await advance(attachment, session=session, settings=settings)
        assert attachment.status == Status.RECEIVED, "should stay put and retry"

    await advance(attachment, session=session, settings=settings)
    assert attachment.status == Status.DEAD

    letter = session.scalars(select(DeadLetter)).one()
    assert letter.stage == "fake"
    assert letter.error_class == "RuntimeError"
    assert letter.attempts == MAX_ATTEMPTS


async def test_non_retryable_failure_dead_letters_immediately(
    session, settings, attachment, swap_stage
) -> None:
    swap_stage(_Fake(Failed(error=ValueError("encrypted"), retryable=False)))
    await advance(attachment, session=session, settings=settings)
    assert attachment.status == Status.DEAD
    assert session.scalars(select(DeadLetter)).one().error_class == "ValueError"


async def test_a_raising_stage_does_not_kill_the_worker(
    session, settings, attachment, swap_stage
) -> None:
    class Exploding(_Fake):
        async def run(self, ctx: StageContext) -> StageOutcome:
            raise OSError("disk gone")

    swap_stage(Exploding(Advanced()))
    outcome = await advance(attachment, session=session, settings=settings)
    assert isinstance(outcome, Failed)
    assert attachment.attempts == 1


async def test_exhausted_attempts_are_not_picked_up_again(session, attachment) -> None:
    attachment.attempts = MAX_ATTEMPTS
    session.flush()
    assert pick_next(session) is None


async def test_llm_is_withheld_from_zero_token_stages(
    session, settings, attachment, swap_stage
) -> None:
    seen: dict[str, object] = {}

    class Recording(_Fake):
        uses_llm: ClassVar[bool] = False

        async def run(self, ctx: StageContext) -> StageOutcome:
            seen["llm"] = ctx.llm
            return Advanced()

    swap_stage(Recording(Advanced()))
    await advance(attachment, session=session, settings=settings, llm=object())  # type: ignore[arg-type]
    assert seen["llm"] is None, "a zero-token stage must not receive an LLM client"


def test_stage_files_are_named_after_their_stage() -> None:
    """One phase, one file, findable by number -- the whole point of the layout."""
    import importlib

    for stage in STAGES:
        module = importlib.import_module(type(stage).__module__)
        assert module.__name__.split(".")[-1].startswith(f"stage_{stage.number:02d}_")
