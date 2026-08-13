"""The only way this codebase talks to a model.

Two invariants, both deliberate:

1. **Every call is metered.** Success, refusal or exception, a row lands in
   ``llm_calls`` with tokens, cache split, USD and latency. No stage calls the
   Anthropic SDK directly, so there is no path that spends money without
   recording it. That is what makes docs/COST_MODEL.md measured rather than
   estimated.

2. **Every call is budgeted.** A per-document ceiling is checked before the
   request goes out. A runaway agent loop costs one stage, not one invoice.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from contract_intake.config import Effort, Settings, get_settings
from contract_intake.db.models import LLMCall
from contract_intake.llm.pricing import Usage, cost_usd, supports_adaptive_thinking

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Base for everything this module raises."""


class RefusalError(LLMError):
    """The model declined. Not retryable with the same prompt."""

    def __init__(self, category: str | None, explanation: str | None) -> None:
        super().__init__(f"model refused (category={category}): {explanation}")
        self.category = category
        self.explanation = explanation


class BudgetExceededError(LLMError):
    """The document already cost more than the configured ceiling."""


class TruncatedError(LLMError):
    """Hit max_tokens before finishing. Retry with more room, not the same call."""


@dataclass(frozen=True, slots=True)
class AgentRun:
    """The result of a bounded tool-use loop."""

    text: str
    usage: Usage
    usd: float
    latency_ms: int
    iterations: int


@dataclass(frozen=True, slots=True)
class LLMResult[TValue]:
    value: TValue
    usage: Usage
    usd: float
    latency_ms: int
    model: str
    stop_reason: str | None


class LLMClient:
    """Metered, budgeted wrapper around the Anthropic API."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session = session
        self._client = client or anthropic.AsyncAnthropic(
            api_key=self._settings.anthropic_api_key.get_secret_value() or None
        )

    # -- public API ---------------------------------------------------------

    async def parse(
        self,
        *,
        purpose: str,
        schema: type[T],
        messages: Sequence[dict[str, Any]],
        system: str | Iterable[dict[str, Any]] | None = None,
        effort: Effort = "high",
        max_tokens: int = 16_000,
        attachment_id: int | None = None,
    ) -> LLMResult[T]:
        """One structured-output call, validated against ``schema``.

        ``output_format`` is merged into ``output_config`` by the SDK, so the
        effort setting survives alongside the JSON schema.
        """
        self._assert_within_budget(attachment_id)
        model = self._settings.model_for(purpose)

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": list(messages),
            "output_format": schema,
        }
        kwargs |= _thinking_params(model, effort)
        if system is not None:
            kwargs["system"] = system

        started = time.perf_counter()
        try:
            response = await self._client.messages.parse(**kwargs)
        except Exception as exc:
            self._record(
                purpose=purpose,
                model=model,
                effort=effort,
                usage=Usage(),
                latency_ms=_elapsed_ms(started),
                attachment_id=attachment_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        latency_ms = _elapsed_ms(started)
        usage = _usage_from(response.usage)
        usd = cost_usd(model, usage)

        self._record(
            purpose=purpose,
            model=model,
            effort=effort,
            usage=usage,
            latency_ms=latency_ms,
            attachment_id=attachment_id,
            ok=response.stop_reason not in ("refusal", "max_tokens"),
            stop_reason=response.stop_reason,
        )

        # Check stop_reason before touching content: on a refusal the content
        # array is empty or partial, and indexing it blindly is the classic bug.
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise RefusalError(
                getattr(details, "category", None),
                getattr(details, "explanation", None),
            )
        if response.stop_reason == "max_tokens":
            raise TruncatedError(f"{purpose}: hit max_tokens={max_tokens}")

        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(f"{purpose}: model returned no parseable output")

        return LLMResult(
            value=parsed,
            usage=usage,
            usd=usd,
            latency_ms=latency_ms,
            model=model,
            stop_reason=response.stop_reason,
        )

    async def run_agent(
        self,
        *,
        purpose: str,
        tools: list[Any],
        messages: Sequence[dict[str, Any]],
        system: str | Iterable[dict[str, Any]] | None = None,
        effort: Effort = "medium",
        max_tokens: int = 8_000,
        max_iterations: int = 12,
        attachment_id: int | None = None,
    ) -> AgentRun:
        """A bounded tool-use loop, metered like everything else.

        Usage is summed across every iteration and written as one ledger row, so
        an agent that took nine round trips reports what it actually cost rather
        than what its last message cost.
        """
        self._assert_within_budget(attachment_id)
        model = self._settings.model_for(purpose)

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": list(messages),
            "tools": tools,
            "max_iterations": max_iterations,
            # The runner resends the whole conversation each iteration, so the
            # history is the thing worth caching -- not just the system prompt.
            # Without this the loop's input cost grows quadratically in turns.
            "cache_control": {"type": "ephemeral"},
        }
        kwargs |= _thinking_params(model, effort)
        if system is not None:
            kwargs["system"] = system

        started = time.perf_counter()
        total = Usage()
        iterations = 0
        text = ""

        try:
            runner = self._client.beta.messages.tool_runner(**kwargs)
            async for message in runner:
                iterations += 1
                total = _add(total, _usage_from(message.usage))
                text = _text_of(message) or text
                if message.stop_reason == "refusal":
                    details = getattr(message, "stop_details", None)
                    raise RefusalError(
                        getattr(details, "category", None),
                        getattr(details, "explanation", None),
                    )
        except Exception as exc:
            self._record(
                purpose=purpose,
                model=model,
                effort=effort,
                usage=total,
                latency_ms=_elapsed_ms(started),
                attachment_id=attachment_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        latency_ms = _elapsed_ms(started)
        self._record(
            purpose=purpose,
            model=model,
            effort=effort,
            usage=total,
            latency_ms=latency_ms,
            attachment_id=attachment_id,
            ok=True,
        )
        return AgentRun(
            text=text,
            usage=total,
            usd=cost_usd(model, total),
            latency_ms=latency_ms,
            iterations=iterations,
        )

    def spent_on(self, attachment_id: int | None) -> float:
        """USD spent on one document so far."""
        if attachment_id is None:
            return 0.0
        stmt = select(func.coalesce(func.sum(LLMCall.usd), 0.0)).where(
            LLMCall.attachment_id == attachment_id
        )
        return float(self._session.scalars(stmt).one())

    # -- internals ----------------------------------------------------------

    def _assert_within_budget(self, attachment_id: int | None) -> None:
        ceiling = self._settings.max_usd_per_document
        spent = self.spent_on(attachment_id)
        if spent >= ceiling:
            raise BudgetExceededError(
                f"attachment {attachment_id} has spent ${spent:.4f}, ceiling is ${ceiling:.2f}"
            )

    def _record(
        self,
        *,
        purpose: str,
        effort: str,
        usage: Usage,
        model: str = "",
        latency_ms: int,
        attachment_id: int | None,
        ok: bool,
        stop_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        self._session.add(
            LLMCall(
                attachment_id=attachment_id,
                purpose=purpose,
                model=model or self._settings.model,
                effort=effort,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                usd=cost_usd(model or self._settings.model, usage),
                latency_ms=latency_ms,
                stop_reason=stop_reason,
                ok=ok,
                error=error,
            )
        )
        self._session.flush()


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens,
    )


def _text_of(message: Any) -> str:
    parts = [
        block.text
        for block in getattr(message, "content", [])
        if getattr(block, "type", None) == "text"
    ]
    return "\n".join(parts).strip()


def _thinking_params(model: str, effort: str) -> dict[str, Any]:
    """Only send thinking and effort to models that accept them."""
    if not supports_adaptive_thinking(model):
        return {}
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _usage_from(raw: Any) -> Usage:
    return Usage(
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
    )
