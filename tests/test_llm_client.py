"""The cost ledger must be unforgeable: no model call may go unrecorded."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select

from contract_intake.config import Settings
from contract_intake.db.models import LLMCall
from contract_intake.llm.client import (
    BudgetExceededError,
    LLMClient,
    RefusalError,
    TruncatedError,
)


class Answer(BaseModel):
    answer: str


@dataclass
class FakeUsage:
    input_tokens: int = 1000
    output_tokens: int = 200
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeResponse:
    parsed_output: Any = None
    stop_reason: str | None = "end_turn"
    usage: FakeUsage = None  # type: ignore[assignment]
    stop_details: Any = None

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = FakeUsage()


class FakeMessages:
    def __init__(self, response: Any = None, raises: Exception | None = None) -> None:
        self._response = response
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._response


class FakeAnthropic:
    def __init__(self, response: Any = None, raises: Exception | None = None) -> None:
        self.messages = FakeMessages(response, raises)


def _client(session, settings: Settings, fake: FakeAnthropic) -> LLMClient:
    return LLMClient(session=session, settings=settings, client=fake)  # type: ignore[arg-type]


async def test_successful_call_is_recorded(session, settings, attachment) -> None:
    fake = FakeAnthropic(FakeResponse(parsed_output=Answer(answer="42")))
    result = await _client(session, settings, fake).parse(
        purpose="extract",
        schema=Answer,
        messages=[{"role": "user", "content": "the question"}],
        attachment_id=attachment.id,
    )

    assert result.value.answer == "42"
    assert result.usd > 0

    row = session.scalars(select(LLMCall)).one()
    assert row.purpose == "extract"
    assert row.attachment_id == attachment.id
    assert row.input_tokens == 1000
    assert row.output_tokens == 200
    assert row.ok is True
    assert row.usd == pytest.approx(result.usd)


async def test_exception_is_still_recorded(session, settings, attachment) -> None:
    fake = FakeAnthropic(raises=RuntimeError("connection reset"))
    with pytest.raises(RuntimeError):
        await _client(session, settings, fake).parse(
            purpose="extract",
            schema=Answer,
            messages=[{"role": "user", "content": "x"}],
            attachment_id=attachment.id,
        )

    row = session.scalars(select(LLMCall)).one()
    assert row.ok is False
    assert "connection reset" in (row.error or "")


async def test_refusal_is_raised_not_parsed(session, settings, attachment) -> None:
    """stop_reason is checked before content, so a refusal cannot be read as data."""

    @dataclass
    class Details:
        category: str = "cyber"
        explanation: str = "declined"

    fake = FakeAnthropic(
        FakeResponse(parsed_output=None, stop_reason="refusal", stop_details=Details())
    )
    with pytest.raises(RefusalError) as exc:
        await _client(session, settings, fake).parse(
            purpose="extract",
            schema=Answer,
            messages=[{"role": "user", "content": "x"}],
            attachment_id=attachment.id,
        )
    assert exc.value.category == "cyber"
    assert session.scalars(select(LLMCall)).one().ok is False


async def test_truncation_is_raised(session, settings, attachment) -> None:
    fake = FakeAnthropic(FakeResponse(parsed_output=Answer(answer="x"), stop_reason="max_tokens"))
    with pytest.raises(TruncatedError):
        await _client(session, settings, fake).parse(
            purpose="extract",
            schema=Answer,
            messages=[{"role": "user", "content": "x"}],
            attachment_id=attachment.id,
        )


async def test_budget_ceiling_blocks_before_spending(session, settings, attachment) -> None:
    session.add(
        LLMCall(
            attachment_id=attachment.id,
            purpose="extract",
            model=settings.model,
            usd=settings.max_usd_per_document + 0.01,
        )
    )
    # Committed, because the ledger is read on its own session now: a pending
    # row in someone else's transaction is not money that has been recorded.
    session.commit()

    fake = FakeAnthropic(FakeResponse(parsed_output=Answer(answer="x")))
    with pytest.raises(BudgetExceededError):
        await _client(session, settings, fake).parse(
            purpose="enrich",
            schema=Answer,
            messages=[{"role": "user", "content": "x"}],
            attachment_id=attachment.id,
        )

    # The blocked call must not have gone out.
    assert fake.messages.calls == []


async def test_effort_survives_alongside_the_schema(session, settings, attachment) -> None:
    """The SDK merges output_format into output_config; effort must not be lost."""
    fake = FakeAnthropic(FakeResponse(parsed_output=Answer(answer="x")))
    await _client(session, settings, fake).parse(
        purpose="extract",
        schema=Answer,
        messages=[{"role": "user", "content": "x"}],
        effort="medium",
        attachment_id=attachment.id,
    )
    sent = fake.messages.calls[0]
    assert sent["output_config"] == {"effort": "medium"}
    assert sent["output_format"] is Answer
    assert sent["thinking"] == {"type": "adaptive"}


async def test_spend_accumulates_per_document(session, settings, attachment) -> None:
    fake = FakeAnthropic(FakeResponse(parsed_output=Answer(answer="x")))
    client = _client(session, settings, fake)
    for _ in range(3):
        await client.parse(
            purpose="extract",
            schema=Answer,
            messages=[{"role": "user", "content": "x"}],
            attachment_id=attachment.id,
        )
    total = session.scalar(select(func.sum(LLMCall.usd)))
    assert client.spent_on(attachment.id) == pytest.approx(total)
