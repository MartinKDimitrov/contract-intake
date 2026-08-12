"""Command line entry points.

``stage`` is the one worth knowing about: it replays a single phase against an
already-persisted document, so extraction can be re-tuned without paying for
loading, and enrichment without paying for extraction.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from sqlalchemy import func, select

from contract_intake.adapters.imap import ImapMailbox
from contract_intake.config import get_settings
from contract_intake.db.engine import init_db, session_scope
from contract_intake.db.models import Attachment, LLMCall
from contract_intake.llm.client import LLMClient
from contract_intake.pipeline.runner import STAGE_BY_NUMBER, STAGES, advance, drain
from contract_intake.pipeline.stage_01_receive import ImapSource


def _cmd_stage(args: argparse.Namespace) -> int:
    stage = STAGE_BY_NUMBER.get(args.number)
    if stage is None:
        known = ", ".join(f"{n:02d}={s.name}" for n, s in sorted(STAGE_BY_NUMBER.items()))
        print(f"unknown stage {args.number}. known: {known}", file=sys.stderr)
        return 2

    with session_scope() as session:
        attachment = session.get(Attachment, args.attachment_id)
        if attachment is None:
            print(f"no attachment {args.attachment_id}", file=sys.stderr)
            return 2
        attachment.status = stage.consumes
        attachment.attempts = 0
        session.flush()
        llm = LLMClient(session) if stage.uses_llm else None
        outcome = asyncio.run(advance(attachment, session=session, llm=llm))
        print(
            f"stage {stage.number:02d} {stage.name}: "
            f"{type(outcome).__name__} -> {attachment.status}"
        )
    return 0


def _cmd_poll(args: argparse.Namespace) -> int:
    """Fetch new mail, then advance everything it produced."""
    source = ImapSource()
    with session_scope() as session:
        created = asyncio.run(source.poll(session, get_settings()))
        print(f"intake: {len(created)} new attachment(s) {created or ''}")
        if created and not args.intake_only:
            moved = asyncio.run(drain(session=session, llm=LLMClient(session)))
            print(f"pipeline: {moved} transition(s)")
    return 0


def _cmd_mailbox(_args: argparse.Namespace) -> int:
    """Connectivity check against the configured folder."""
    print(json.dumps(ImapMailbox(get_settings()).probe(), indent=2))
    return 0


def _cmd_drain(_args: argparse.Namespace) -> int:
    with session_scope() as session:
        moved = asyncio.run(drain(session=session, llm=LLMClient(session)))
    print(f"{moved} transition(s)")
    return 0


def _cmd_costs(_args: argparse.Namespace) -> int:
    with session_scope() as session:
        rows = session.execute(
            select(
                LLMCall.purpose,
                func.count(),
                func.sum(LLMCall.usd),
                func.sum(LLMCall.input_tokens),
                func.sum(LLMCall.cache_read_tokens),
            ).group_by(LLMCall.purpose)
        ).all()
        total = session.scalar(select(func.coalesce(func.sum(LLMCall.usd), 0.0))) or 0.0
    summary = {"total_usd": round(total, 6), "by_purpose": [list(r) for r in rows]}
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_stages(_args: argparse.Namespace) -> int:
    print("01 receive        (Source)  ->  received")
    for s in STAGES:
        token = "LLM" if s.uses_llm else " 0 "
        print(f"{s.number:02d} {s.name:<14} [{token}]  {s.consumes}  ->  {s.produces}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    init_db(get_settings())

    parser = argparse.ArgumentParser(prog="contract-intake")
    sub = parser.add_subparsers(dest="command", required=True)

    p_stage = sub.add_parser("stage", help="replay one pipeline phase")
    p_stage.add_argument("--number", type=int, required=True)
    p_stage.add_argument("--attachment-id", type=int, required=True)
    p_stage.set_defaults(func=_cmd_stage)

    p_poll = sub.add_parser("poll", help="fetch new mail and run the pipeline")
    p_poll.add_argument("--intake-only", action="store_true", help="stop after stage 01")
    p_poll.set_defaults(func=_cmd_poll)

    sub.add_parser("mailbox", help="IMAP connectivity check").set_defaults(func=_cmd_mailbox)
    sub.add_parser("drain", help="advance all pending work").set_defaults(func=_cmd_drain)
    sub.add_parser("costs", help="print the cost ledger").set_defaults(func=_cmd_costs)
    sub.add_parser("stages", help="print the pipeline").set_defaults(func=_cmd_stages)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
