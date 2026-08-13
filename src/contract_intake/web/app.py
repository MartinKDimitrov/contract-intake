"""HTTP surface: health, cost ledger, review queue.

Kept thin on purpose -- it renders what the pipeline produced and never contains
business logic. There is no inbound webhook route: `WebhookSource` exists in stage 01 as the
seam for one, and nothing is wired to HTTP.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from contract_intake.config import get_settings
from contract_intake.db.engine import init_db, session_scope
from contract_intake.db.models import Attachment, Contract, LLMCall
from contract_intake.status import Status
from contract_intake.web import review

app = FastAPI(title="contract-intake", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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


# -- the review queue -------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def review_queue(request: Request, state: str = "open") -> Any:
    with session_scope() as session:
        rows = review.queue(session, state=state)
    return templates.TemplateResponse(request, "queue.html", {"rows": rows, "state": state})


@app.get("/review/{item_id}", response_class=HTMLResponse)
def review_item(request: Request, item_id: int) -> Any:
    with session_scope() as session:
        view = review.load_item(session, item_id)
        if view is None:
            raise HTTPException(status_code=404, detail="no such review item")
        return templates.TemplateResponse(request, "item.html", {"view": view})


@app.post("/review/{item_id}/resolve")
async def resolve(item_id: int, request: Request, action: str = Form(...)) -> RedirectResponse:
    """Close a review item, carrying whatever the reviewer changed.

    The corrections are read from the form rather than declared as parameters:
    which fields a reviewer may edit is a property of the extraction schema, and
    duplicating that list here is how the two drift. Anything posted under
    ``field:<name>`` and differing from the extracted value is a correction.
    """
    if action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="action must be approve or reject")

    form = await request.form()
    with session_scope() as session:
        corrections = review.corrections_from_form(
            session, item_id, {k: v for k, v in form.items() if isinstance(v, str)}
        )
        if review.resolve_item(session, item_id, action=action, corrections=corrections) is None:
            raise HTTPException(status_code=404, detail="no such review item")
    return RedirectResponse(f"/review/{item_id}", status_code=303)


@app.get("/contracts")
def contracts() -> list[dict[str, Any]]:
    """Everything that made it through, machine-approved or human-approved."""
    with session_scope() as session:
        rows = session.scalars(select(Contract).order_by(Contract.id.desc())).all()
        return [
            {
                "id": c.id,
                "counterparty": c.counterparty_name,
                "counterparty_id": c.counterparty_id,
                "approved_by_human": bool(c.payload.get("_approved_by_human")),
                "terms": {k: v for k, v in c.payload.items() if not k.startswith("_")},
            }
            for c in rows
        ]
