"""The pipeline index and its driver.

If you read one file to understand this system, read ``STAGES`` below: it is the
whole flow, in order, on one screen.

There is no message broker. ``attachments.status`` is the queue -- the driver
picks the attachment furthest along the pipeline and hands it to the stage
that consumes that status. Consequences: a crash resumes exactly where it
stopped, any single phase can be replayed in isolation, and the entire system
state is one `sqlite3` query away.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import case, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, SessionTransaction

from contract_intake.config import Settings, get_settings
from contract_intake.db.engine import session_scope
from contract_intake.db.models import Attachment, DeadLetter, LLMCall
from contract_intake.llm.client import LLMClient
from contract_intake.pipeline.base import (
    Advanced,
    Failed,
    Rejected,
    Stage,
    StageContext,
    StageOutcome,
)
from contract_intake.pipeline.stage_01_receive import ImapSource
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

    Both ends count. `pairwise` covers only the interior seams, so intake could
    produce a status no stage consumes -- every new attachment landing somewhere
    `pick_next` never looks -- and the last stage could produce a non-terminal
    status, sending documents round the pipeline again with `attempts` reset on
    each pass.
    """
    if stages and ImapSource.produces != stages[0].consumes:
        raise BrokenPipelineError(
            f"ImapSource produces {ImapSource.produces!r} but stage "
            f"{stages[0].number} ({stages[0].name}) consumes {stages[0].consumes!r}"
        )

    if stages and not is_terminal(stages[-1].produces):
        raise BrokenPipelineError(
            f"the last stage produces {stages[-1].produces!r}, which is not terminal -- "
            "documents would cycle back through the pipeline forever"
        )

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


#: How far along the pipeline each runnable status is. Used to order the queue.
PROGRESS: Final = {stage.consumes.value: index for index, stage in enumerate(STAGES)}


def pick_next(session: Session) -> Attachment | None:
    """The attachment closest to the end of the pipeline, oldest first within that.

    Ordering by ``updated_at`` alone is a plausible queue and the wrong one. It
    advances every document in lockstep, so a batch of thirty moves together
    from stage to stage and *nothing finishes* until the whole batch does. A
    poll of thirteen real documents spent its entire transition budget without
    producing a single contract row.

    Finishing what is in flight before starting more work is the standard
    answer, and here it also bounds cost: a document that reaches stage 06 has
    already been paid for, and leaving it unfinished wastes what it cost.
    """
    stmt = (
        select(Attachment)
        .where(Attachment.status.in_(tuple(STAGE_BY_CONSUMES)))
        .where(Attachment.attempts < MAX_ATTEMPTS)
        .where((Attachment.retry_after.is_(None)) | (Attachment.retry_after <= datetime.now(UTC)))
        .order_by(case(PROGRESS, value=Attachment.status, else_=0).desc(), Attachment.updated_at)
        .limit(1)
    )
    return session.scalars(stmt).first()


async def advance(
    attachment: Attachment,
    *,
    session: Session,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    stage: Stage | None = None,
) -> StageOutcome:
    """Run exactly one stage against one attachment and persist the result.

    The stage is normally chosen by the attachment's status; passing one
    explicitly is for replay and for tests.
    """
    settings = settings or get_settings()
    stage = stage or STAGE_BY_CONSUMES.get(Status(attachment.status))
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

    # A savepoint around the stage, so a failure takes its half-written output
    # with it. Without one, a stage that wrote an `enrichments` row and then
    # raised left that row committed by `drain`, and three attempts produced
    # three rows and three paid agent runs for one document.
    savepoint = session.begin_nested()
    try:
        outcome = await stage.run(ctx)
    except Exception as exc:  # a stage that raises is treated as a failure
        log.exception("stage %s raised on attachment %s", stage.name, attachment.id)
        outcome = Failed(error=exc, retryable=is_retryable(exc))

    spent = list(getattr(llm, "recorded", ()))
    if isinstance(outcome, Failed):
        # The stage's writes go; what it spent does not. The savepoint rollback
        # would take the ledger rows with it, and the retry would then spend
        # again against a ceiling that had forgotten the first attempt.
        _discard(savepoint)
        for fields in spent:
            session.add(LLMCall(**fields))
    elif savepoint.is_active:
        savepoint.commit()

    _persist(attachment, stage, outcome, session, spent=spent)

    if llm is not None and hasattr(llm, "recorded"):
        # After _persist, not before: its recovery path rolls the session back
        # and needs these rows to replay. Cleared first, a database-level stage
        # failure lost the record of a paid call -- the one case the buffering
        # exists for.
        llm.recorded.clear()
    return outcome


def _discard(savepoint: SessionTransaction) -> None:
    """Roll back the stage's writes, tolerating a session already past saving."""
    try:
        if savepoint.is_active:
            savepoint.rollback()
    except SQLAlchemyError:
        log.exception("could not roll back the stage savepoint")


#: Exceptions that mean "the same input will fail the same way". Everything else
#: is retried, because a 429, a timeout and a dropped connection all recover on
#: their own. Distinguishing them matters: a bug used to burn three attempts and
#: two and a half minutes of backoff -- and for stages 04 and 05, three paid
#: model calls -- before anyone saw the traceback.
PERMANENT_ERRORS: tuple[type[BaseException], ...] = (
    TypeError,
    AttributeError,
    KeyError,
    IndexError,
    NotImplementedError,
    ImportError,
)


def is_retryable(exc: BaseException) -> bool:
    return not isinstance(exc, PERMANENT_ERRORS)


def _persist(
    attachment: Attachment,
    stage: Stage,
    outcome: StageOutcome,
    session: Session,
    spent: list[dict[str, Any]] | None = None,
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
        case Failed(error=error, retryable=retryable, note=note):
            attachment.attempts += 1
            exhausted = not retryable or attachment.attempts >= MAX_ATTEMPTS
            attachment.retry_after = None if exhausted else _backoff_for(attachment.attempts)
            # Otherwise `status_reason` still reads like the last success, and
            # `select status, status_reason, attempts` -- the advertised way to
            # inspect this system -- shows a stale note beside attempts=1.
            attachment.status_reason = f"{type(error).__name__}: {error}" + (
                f" ({note})" if note else ""
            )
            if exhausted:
                attachment.status = Status.DEAD
                session.add(
                    DeadLetter(
                        attachment_id=attachment.id,
                        stage=stage.name,
                        error_class=type(error).__name__,
                        message=str(error),
                        attempts=attachment.attempts,
                    )
                )

    # Read before the flush. A failed flush expires the instance, so touching
    # any attribute afterwards triggers a refresh on a deactivated session and
    # raises -- out of the recovery block, out of advance(), and out of drain().
    attachment_id = attachment.id
    try:
        session.flush()
    except SQLAlchemyError:
        # The stage's failure *was* a database failure, so this session cannot
        # record anything -- including the attempt counter that MAX_ATTEMPTS
        # depends on. Left here, the row keeps its old status and `pick_next`
        # hands it straight back, forever, at full speed. Book-keeping goes on a
        # clean session instead, which is the one place a fresh one is worth the
        # cost.
        log.exception("could not persist the outcome for attachment %s", attachment_id)
        # Detach before rolling back: the book-keeping this session could not
        # write is about to be redone on a clean one, and SQLAlchemy is right to
        # warn about discarding pending state we no longer want.
        session.expunge(attachment)
        session.rollback()
        _persist_on_a_clean_session(attachment_id, stage, outcome, spent or [])


def _persist_on_a_clean_session(
    attachment_id: int, stage: Stage, outcome: StageOutcome, spent: list[dict[str, Any]]
) -> None:
    try:
        with session_scope() as fresh:
            row = fresh.get(Attachment, attachment_id)
            if row is None:
                return
            # The paid calls go on this session too. The rollback above took the
            # replayed rows with it, and nothing else will write them.
            for fields in spent:
                fresh.add(LLMCall(**fields))
            _persist(row, stage, outcome, fresh)
    except SQLAlchemyError:
        log.exception("could not record the failure for attachment %s at all", attachment_id)


@dataclass(frozen=True, slots=True)
class DrainResult:
    """What a drain did. ``finished`` is the number a caller actually cares about."""

    transitions: int
    finished: int

    def __str__(self) -> str:
        return f"{self.transitions} transition(s), {self.finished} document(s) finished"


async def drain(
    *,
    session: Session,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    max_transitions: int = 500,
) -> DrainResult:
    """Advance work until nothing is runnable.

    ``max_transitions`` is a runaway guard, not a budget. It used to default to
    100, which reads like a sensible batch size and is not one: it counts
    *transitions*, each document needs six of them, and the queue advanced every
    document by one step before advancing any of them by two. Thirteen documents
    exhausted it with nothing delivered and eleven left paid for but unfinished.

    It is not unbounded either. There is one spin this cannot otherwise stop --
    a row whose failure the database itself refuses to record, so `attempts`
    never rises and `MAX_ATTEMPTS` never engages -- and in that state every
    transition on stages 04 and 05 is a paid call. Five hundred bounds the
    damage while still clearing any real backlog in one pass.

    Real spending limits belong where money is spent: `max_usd_per_document`,
    checked before every model call and on every agent iteration.
    """
    moved = finished = 0
    while moved < max_transitions:
        attachment = pick_next(session)
        if attachment is None:
            break
        await advance(attachment, session=session, settings=settings, llm=llm)
        session.commit()
        moved += 1
        if is_terminal(Status(attachment.status)):
            finished += 1
    else:
        if pick_next(session) is None:
            return DrainResult(transitions=moved, finished=finished)
        log.error(
            "drain stopped at the %s-transition guard with work still runnable. "
            "At the default that means a loop rather than a backlog; run again "
            "and check whether the same attachment is moving.",
            max_transitions,
        )
    return DrainResult(transitions=moved, finished=finished)
