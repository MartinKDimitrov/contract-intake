"""The pipeline index and its driver.

If you read one file to understand this system, read ``STAGES`` below: it is the
whole flow, in order, on one screen.

There is no message broker. ``attachments.status`` is the queue -- the driver
picks the oldest attachment in a non-terminal status and hands it to the stage
that consumes that status. Consequences: a crash resumes exactly where it
stopped, any single phase can be replayed in isolation, and the entire system
state is one `sqlite3` query away.
"""

from __future__ import annotations

import itertools
import logging
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from contract_intake.config import Settings, get_settings
from contract_intake.db.models import Attachment, DeadLetter
from contract_intake.llm.client import LLMClient
from contract_intake.pipeline.base import (
    Advanced,
    Failed,
    Rejected,
    Stage,
    StageContext,
    StageOutcome,
)
from contract_intake.pipeline.stage_02_triage import TriageStage
from contract_intake.pipeline.stage_03_load import LoadStage
from contract_intake.pipeline.stage_04_extract import ExtractStage
from contract_intake.pipeline.stage_05_enrich import EnrichStage
from contract_intake.pipeline.stage_06_decide import DecideStage
from contract_intake.pipeline.stage_07_deliver import DeliverStage
from contract_intake.status import Status, is_terminal

log = logging.getLogger(__name__)

MAX_ATTEMPTS: Final = 3

#: Backoff between attempts, in seconds: 30s, then 2m, then the row is dead.
#: Short enough that a transient blip clears on its own, long enough that a rate
#: limit is not answered with another request immediately.
BACKOFF_SECONDS: Final = (30, 120, 300)


def _backoff_for(attempts: int) -> datetime:
    index = min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)
    return datetime.now(UTC) + timedelta(seconds=BACKOFF_SECONDS[index])


#: The pipeline. Stage 01 (intake) is a Source and lives outside this chain --
#: see base.py for why.
STAGES: Final[tuple[Stage, ...]] = (
    TriageStage(),
    LoadStage(),
    ExtractStage(),
    EnrichStage(),
    DecideStage(),
    DeliverStage(),
)

STAGE_BY_NUMBER: Final[dict[int, Stage]] = {s.number: s for s in STAGES}
STAGE_BY_CONSUMES: Final[dict[Status, Stage]] = {s.consumes: s for s in STAGES}


class BrokenPipelineError(RuntimeError):
    """The stage chain is not contiguous -- a programming error, caught at import."""


def validate_chain(stages: tuple[Stage, ...] = STAGES) -> None:
    """Assert every stage hands off to the next one.

    Guards the failure mode that stage-per-file introduces: renaming a status in
    one file and forgetting its neighbour, producing a document that advances
    into a status nobody consumes and silently stalls forever.
    """
    for current, following in itertools.pairwise(stages):
        if current.produces != following.consumes:
            raise BrokenPipelineError(
                f"stage {current.number} ({current.name}) produces "
                f"{current.produces!r} but stage {following.number} "
                f"({following.name}) consumes {following.consumes!r}"
            )
    numbers = [s.number for s in stages]
    if numbers != sorted(numbers):
        raise BrokenPipelineError(f"stages are out of order: {numbers}")


validate_chain()


def pick_next(session: Session) -> Attachment | None:
    """Oldest attachment sitting in a status some stage can advance."""
    stmt = (
        select(Attachment)
        .where(Attachment.status.in_(tuple(STAGE_BY_CONSUMES)))
        .where(Attachment.attempts < MAX_ATTEMPTS)
        .where((Attachment.retry_after.is_(None)) | (Attachment.retry_after <= datetime.now(UTC)))
        .order_by(Attachment.updated_at)
        .limit(1)
    )
    return session.scalars(stmt).first()


async def advance(
    attachment: Attachment,
    *,
    session: Session,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
) -> StageOutcome:
    """Run exactly one stage against one attachment and persist the result."""
    settings = settings or get_settings()
    stage = STAGE_BY_CONSUMES.get(Status(attachment.status))
    if stage is None:
        raise BrokenPipelineError(
            f"attachment {attachment.id} is in status {attachment.status!r}, "
            "which no stage consumes"
        )

    ctx = StageContext(
        attachment_id=attachment.id,
        session=session,
        settings=settings,
        llm=llm if stage.uses_llm else None,
    )

    try:
        outcome = await stage.run(ctx)
    except Exception as exc:  # a stage that raises is treated as a retryable failure
        log.exception("stage %s raised on attachment %s", stage.name, attachment.id)
        outcome = Failed(error=exc, retryable=True)

    _persist(attachment, stage, outcome, session)
    return outcome


def _persist(
    attachment: Attachment,
    stage: Stage,
    outcome: StageOutcome,
    session: Session,
) -> None:
    match outcome:
        case Advanced():
            attachment.status = stage.produces
            attachment.status_reason = outcome.note or None
            attachment.attempts = 0
            attachment.retry_after = None
        case Rejected(reason=reason):
            attachment.status = Status.REJECTED
            attachment.status_reason = reason
        case Failed(error=error, retryable=retryable):
            attachment.attempts += 1
            exhausted = not retryable or attachment.attempts >= MAX_ATTEMPTS
            attachment.retry_after = None if exhausted else _backoff_for(attachment.attempts)
            if exhausted:
                attachment.status = Status.DEAD
                attachment.status_reason = f"{type(error).__name__}: {error}"
                session.add(
                    DeadLetter(
                        attachment_id=attachment.id,
                        stage=stage.name,
                        error_class=type(error).__name__,
                        message=str(error),
                        attempts=attachment.attempts,
                    )
                )
    session.flush()


async def drain(
    *,
    session: Session,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    limit: int = 100,
) -> int:
    """Advance work until nothing is runnable. Returns the number of transitions."""
    moved = 0
    for _ in range(limit):
        attachment = pick_next(session)
        if attachment is None:
            break
        await advance(attachment, session=session, settings=settings, llm=llm)
        session.commit()
        moved += 1
        if is_terminal(Status(attachment.status)):
            continue
    return moved
