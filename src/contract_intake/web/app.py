"""HTTP surface: health, cost ledger, review queue, inbound webhook.

Kept thin on purpose -- it renders what the pipeline produced and never contains
business logic. The review queue itself arrives in phase 5.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from sqlalchemy import func, select

from contract_intake.config import get_settings
from contract_intake.db.engine import init_db, session_scope
from contract_intake.db.models import Attachment, LLMCall
from contract_intake.status import Status

app = FastAPI(title="contract-intake", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init_db(get_settings())


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    with session_scope() as session:
        rows = session.execute(
            select(Attachment.status, func.count()).group_by(Attachment.status)
        ).all()
    return {
        "ok": True,
        "model": get_settings().model,
        "attachments_by_status": {str(status): count for status, count in rows},
    }


@app.get("/metrics/costs")
def costs() -> dict[str, Any]:
    """The cost ledger, aggregated. Backs the table in README.md."""
    with session_scope() as session:
        by_purpose = session.execute(
            select(
                LLMCall.purpose,
                func.count(),
                func.sum(LLMCall.usd),
                func.sum(LLMCall.input_tokens),
                func.sum(LLMCall.output_tokens),
                func.sum(LLMCall.cache_read_tokens),
                func.sum(LLMCall.cache_write_tokens),
            ).group_by(LLMCall.purpose)
        ).all()

        delivered = (
            session.scalar(
                select(func.count())
                .select_from(Attachment)
                .where(Attachment.status == Status.DELIVERED)
            )
            or 0
        )
        total_usd = session.scalar(select(func.coalesce(func.sum(LLMCall.usd), 0.0))) or 0.0

    stages = [
        {
            "purpose": purpose,
            "calls": calls,
            "usd": round(usd or 0.0, 6),
            "input_tokens": inp or 0,
            "output_tokens": out or 0,
            "cache_read_tokens": cr or 0,
            "cache_write_tokens": cw or 0,
            "cache_hit_ratio": round((cr or 0) / max(1, (inp or 0) + (cr or 0) + (cw or 0)), 3),
        }
        for purpose, calls, usd, inp, out, cr, cw in by_purpose
    ]
    return {
        "total_usd": round(total_usd, 6),
        "documents_delivered": delivered,
        "usd_per_document": round(total_usd / delivered, 6) if delivered else None,
        "by_stage": stages,
    }
