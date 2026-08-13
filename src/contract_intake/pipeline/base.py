"""The stage contract.

Every phase of the pipeline implements ``Stage``: it consumes an attachment in
one status and leaves it in the next one. The uniform shape is what lets the
worker be a five-line loop, lets any single phase be replayed in isolation
(``make stage N=04 ID=17``), and makes a crash resumable.

One deliberate exception: stage 01 does not fit this contract. It consumes no
attachment -- it *creates* them, fanning one email out into N rows. Rather than
distort ``Stage`` to accommodate it, intake is modelled separately as
``Source``. That asymmetry is real and is recorded in docs/TRADEOFFS.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from contract_intake.config import Settings
from contract_intake.llm.client import LLMClient
from contract_intake.status import Status


@dataclass(frozen=True, slots=True)
class StageContext:
    """Everything a stage is allowed to touch.

    Stages receive their dependencies here rather than reaching for globals, so
    a test can drive any phase with an in-memory database and a fake LLM.
    """

    # fmt: off
    attachment_id : int
    session       : Session
    settings      : Settings
    llm           : LLMClient | None = None
    # fmt: on
    """None for the zero-token stages (02, 03, 06, 07).

    Stage 01 is a Source and never receives a context at all.
    """


@dataclass(frozen=True, slots=True)
class Advanced:
    """The document moved forward. The worker writes ``status`` and continues."""

    note: str = ""
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Rejected:
    """An expected, final 'no' -- not a contract, encrypted, oversized.

    Terminal and *not* an error: no retry, no dead letter, no alert.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class Failed:
    """Something went wrong. ``retryable`` decides retry vs dead letter."""

    # fmt: off
    error     : Exception
    retryable : bool = True
    note      : str  = ""
    # fmt: on


StageOutcome = Advanced | Rejected | Failed


@runtime_checkable
class Stage(Protocol):
    """A single phase. One file per implementation, in stage order."""

    # fmt: off
    number   : ClassVar[int]
    name     : ClassVar[str]
    consumes : ClassVar[Status]
    produces : ClassVar[Status]
    uses_llm : ClassVar[bool]
    # fmt: on

    async def run(self, ctx: StageContext) -> StageOutcome: ...


@runtime_checkable
class Source(Protocol):
    """Intake. Produces new attachments rather than advancing existing ones."""

    name: ClassVar[str]
    produces: ClassVar[Status]

    async def poll(self, session: Session, settings: Settings) -> Sequence[int]:
        """Fetch what is new and return the attachment ids created."""
        ...
