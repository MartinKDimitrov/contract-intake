"""The three guards that stand between a loop and an unbounded bill.

All three were written in one afternoon and none had a test. That is the worst
possible combination for code whose whole job is to be there when something else
goes wrong, so each one is pinned here against the failure it was written for.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from sqlalchemy import func, select

from contract_intake.db.models import LLMCall
from contract_intake.llm.client import BudgetExceededError, LLMClient
from contract_intake.pipeline.runner import PERMANENT_ERRORS, is_retryable


class _Usage:
    input_tokens = 40_000
    output_tokens = 2_000
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Message:
    stop_reason = "end_turn"
    usage = _Usage()
    content: ClassVar[list[Any]] = []


class _EndlessRunner:
    """A tool runner that would happily go round forever."""

    def __init__(self, iterations: int = 50) -> None:
        self.iterations = iterations
        self.served = 0

    def __aiter__(self) -> _EndlessRunner:
        return self

    async def __anext__(self) -> _Message:
        if self.served >= self.iterations:
            raise StopAsyncIteration
        self.served += 1
        return _Message()


class _FakeMessages:
    def __init__(self, runner: _EndlessRunner) -> None:
        self._runner = runner

    def tool_runner(self, **_kwargs: Any) -> _EndlessRunner:
        return self._runner


class _FakeBeta:
    def __init__(self, runner: _EndlessRunner) -> None:
        self.messages = _FakeMessages(runner)


class _FakeProvider:
    def __init__(self, runner: _EndlessRunner) -> None:
        self.beta = _FakeBeta(runner)


# -- the per-iteration budget check -----------------------------------------


async def test_the_agent_loop_stops_when_the_document_runs_out_of_budget(
    session, settings, attachment
) -> None:
    """Checked once on entry, a ceiling bounds the call *after* the loop.

    Twelve iterations at forty thousand input tokens each spend several dollars
    against a ceiling of well under one, and the entry check -- reading a spend
    of zero -- waves every one of them through.
    """
    runner = _EndlessRunner(iterations=50)
    client = LLMClient(session, settings, _FakeProvider(runner))  # type: ignore[arg-type]

    with pytest.raises(BudgetExceededError, match="iteration"):
        await client.run_agent(
            purpose="enrich",
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            effort="medium",
            attachment_id=attachment.id,
        )

    assert runner.served < 50, "the loop must stop before the provider does"


async def test_a_short_agent_run_inside_the_budget_is_not_interrupted(
    session, settings, attachment
) -> None:
    """The control. A guard that fires on everything is not a guard."""
    client = LLMClient(session, settings, _FakeProvider(_EndlessRunner(iterations=1)))  # type: ignore[arg-type]

    run = await client.run_agent(
        purpose="enrich",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        effort="medium",
        attachment_id=attachment.id,
    )

    assert run.iterations == 1


# -- the independent ledger --------------------------------------------------


async def test_a_failed_stage_still_leaves_what_it_spent_on_the_ledger(
    session, settings, attachment
) -> None:
    """Money already billed must not be forgotten because a stage failed.

    Otherwise the retry spends again against a ceiling that has forgotten the
    first attempt, and three attempts cost three times the ceiling.

    The first attempt at this wrote the row on its own connection. Under WAL
    that advances the write-ahead log while the caller holds a read snapshot,
    and SQLite then refuses the caller's own write -- it failed every document
    at stage 04 with "database is locked". So the rows are buffered and the
    runner replays them past the rollback instead.
    """
    from contract_intake.pipeline import runner as runner_module
    from contract_intake.pipeline.base import Failed, StageContext, StageOutcome
    from contract_intake.status import Status

    class SpendsThenFails:
        number, name = 5, "enrich"
        consumes, produces = Status.EXTRACTED, Status.ENRICHED
        uses_llm = True

        async def run(self, ctx: StageContext) -> StageOutcome:
            await client.run_agent(
                purpose="enrich",
                messages=[{"role": "user", "content": "x"}],
                tools=[],
                effort="medium",
                attachment_id=ctx.attachment_id,
            )
            return Failed(error=RuntimeError("declined"), retryable=True)

    client = LLMClient(session, settings, _FakeProvider(_EndlessRunner(iterations=1)))  # type: ignore[arg-type]
    attachment.status = Status.EXTRACTED
    session.commit()

    await runner_module.advance(
        attachment, session=session, settings=settings, llm=client, stage=SpendsThenFails()
    )
    session.commit()

    recorded = session.scalar(
        select(func.count()).select_from(LLMCall).where(LLMCall.attachment_id == attachment.id)
    )
    assert recorded == 1, "a paid call must survive the rollback of the work that made it"
    assert client.spent_on(attachment.id) > 0.0


# -- retryable versus permanent ---------------------------------------------


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (TimeoutError("provider slow"), True),
        (ConnectionError("reset"), True),
        (OSError("disk hiccup"), True),
        (AttributeError("'NoneType' object has no attribute 'value'"), False),
        (TypeError("unhashable"), False),
        (KeyError("missing"), False),
        (NotImplementedError(), False),
    ],
)
def test_a_bug_is_not_mistaken_for_a_bad_afternoon(error: Exception, retryable: bool) -> None:
    """Everything used to be retryable, so a bug burned three attempts.

    For the two stages that call a model that is three paid calls and two and a
    half minutes of backoff before anyone sees the traceback.
    """
    assert is_retryable(error) is retryable


def test_the_permanent_list_is_only_programmer_errors() -> None:
    """A transient failure in this list would strand documents that should recover."""
    for kind in PERMANENT_ERRORS:
        assert issubclass(kind, Exception)
        assert not issubclass(kind, TimeoutError | ConnectionError | OSError), kind
