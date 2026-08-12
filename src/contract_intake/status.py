"""The pipeline state machine.

An attachment's ``status`` column *is* the work queue. There is no broker: the
worker picks up the oldest row in a non-terminal status and hands it to the
stage that consumes that status. A crash mid-document therefore resumes exactly
where it stopped, and the whole system state is inspectable with `sqlite3`.

    RECEIVED -> TRIAGED -> LOADED -> EXTRACTED -> ENRICHED -> DECIDED -> DELIVERED
         |          |         |          |           |          |
         +----------+---------+----------+-----------+----------+--> REJECTED
         |                                                           (expected: not a contract)
         +----------+---------+----------+-----------+----------+--> DEAD
                                                                     (unexpected: retries exhausted)

See docs/ARCHITECTURE.md for what each transition costs in tokens.
"""

from __future__ import annotations

from enum import StrEnum


class Status(StrEnum):
    RECEIVED = "received"
    TRIAGED = "triaged"
    LOADED = "loaded"
    EXTRACTED = "extracted"
    ENRICHED = "enriched"
    DECIDED = "decided"
    DELIVERED = "delivered"

    # Terminal.
    REJECTED = "rejected"
    DEAD = "dead"


TERMINAL: frozenset[Status] = frozenset({Status.DELIVERED, Status.REJECTED, Status.DEAD})


def is_terminal(status: Status) -> bool:
    return status in TERMINAL


class Route(StrEnum):
    """What stage 06 decided to do with a document."""

    AUTO_APPROVED = "auto_approved"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
