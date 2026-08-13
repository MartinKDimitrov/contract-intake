"""The one place a contract record is written.

Two paths reach this table and they are not variations on a theme -- stage 07
files a contract that passed every rule, and the review UI files one a person
approved after looking at it. Both produce the same row, dedupe on the same key
and flatten the same payload, and for a while both did it in their own words,
in two files, with the flattening written out twice.

They had already drifted: only the reviewer's copy carried the human's
corrections and the marker saying a person had signed off. A schema change to
``contracts`` needed editing in two places, and nothing pointed from one to the
other.

The difference between the two is one argument.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from contract_intake.db.models import Contract


def flatten(fields: dict[str, Any]) -> dict[str, Any]:
    """Reduce extracted fields to plain values for downstream consumers.

    The provenance stays in ``extractions``. A system reading ``contracts``
    wants the terms, and anything that reached this table has already been
    checked -- by the rules, by a person, or by both.
    """
    return {
        name: (entry.get("value") if isinstance(entry, dict) else entry)
        for name, entry in fields.items()
        if not name.startswith("_")
    }


def record(
    session: Session,
    *,
    decision_id: int,
    fields: dict[str, Any],
    counterparty_id: str | None,
    corrections: dict[str, Any] | None = None,
) -> int:
    """File the contract for one decision, or return the one already filed.

    Idempotent on ``decision_id``, which is also a unique constraint -- replaying
    stage 07, or a reviewer double-clicking approve, cannot produce two records.

    ``corrections`` is what a human changed. Passing any marks the record as
    human-approved, because a value a person typed and a value a model read are
    not the same kind of fact and a downstream consumer is entitled to know
    which it is holding.
    """
    existing = session.scalar(select(Contract).where(Contract.decision_id == decision_id))
    if existing is not None:
        return existing.id

    payload = flatten(fields)
    if corrections is not None:
        payload |= corrections
        payload["_approved_by_human"] = True

    row = Contract(
        decision_id=decision_id,
        counterparty_id=counterparty_id,
        counterparty_name=str(payload.get("counterparty_name") or ""),
        payload=payload,
    )
    session.add(row)
    session.flush()
    return row.id
