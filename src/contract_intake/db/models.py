"""Persistence schema.

One table per pipeline artefact, so that every stage's output is durable and
independently inspectable. Two tables carry extra weight:

* ``attachments.status`` drives the whole state machine (see status.py).
* ``llm_calls`` is the cost ledger. *Every* model call writes a row here, with
  no exception path -- it is what makes docs/COST_MODEL.md measured rather than
  estimated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from contract_intake.status import Route, Status

SCHEMA_VERSION = 2


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


class Email(Base):
    """One inbound message. Deduplicated on RFC-822 Message-ID."""

    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(998), unique=True, index=True)
    sender: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    source: Mapped[str] = mapped_column(String(16), default="imap")  # imap | webhook

    attachments: Mapped[list[Attachment]] = relationship(back_populates="email")


class Attachment(Base):
    """A candidate document. Its ``status`` is the pipeline's work queue."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    #: Unique, not merely indexed. The dedup in stage 01 is a read-then-write,
    #: so without a constraint two pollers both see "not seen" and both insert.
    sha256: Mapped[str] = mapped_column(String(64), index=True, unique=True)
    declared_mime: Mapped[str] = mapped_column(String(128), default="")
    detected_mime: Mapped[str] = mapped_column(String(128), default="")
    size_bytes: Mapped[int] = mapped_column(Integer)
    stored_path: Mapped[str] = mapped_column(String(1024))

    status: Mapped[Status] = mapped_column(String(16), default=Status.RECEIVED, index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    retry_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    """Not runnable before this. Set on a retryable failure so a rate limit or a
    flapping dependency is backed off rather than hammered on the next pass."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    email: Mapped[Email] = relationship(back_populates="attachments")

    __table_args__ = (Index("ix_attachments_status_updated", "status", "updated_at"),)


class Document(Base):
    """Stage 03 output: pages resolved to text or queued for vision."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    attachment_id: Mapped[int] = mapped_column(ForeignKey("attachments.id"), index=True)
    page_count: Mapped[int] = mapped_column(Integer)
    text_pages: Mapped[int] = mapped_column(Integer, default=0)
    image_pages: Mapped[int] = mapped_column(Integer, default=0)
    pages: Mapped[list[Any]] = mapped_column(default=list)
    #: Masked items by category, or NULL when masking did not run. The
    #: distinction matters: an empty dict means the document was clean, NULL
    #: means nothing looked.
    redactions: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Extraction(Base):
    """Stage 04 output: structured fields, each with confidence and provenance."""

    __tablename__ = "extractions"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=SCHEMA_VERSION)
    fields: Mapped[dict[str, Any]] = mapped_column(default=dict)
    model: Mapped[str] = mapped_column(String(64))
    effort: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Enrichment(Base):
    """Stage 05 output: agent findings plus the full tool trace.

    ``tool_trace`` is deliberately persisted in full: it is the evidence that the
    knowledge base changed the outcome rather than decorating it, and the review
    UI renders it next to each finding.
    """

    __tablename__ = "enrichments"

    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[int] = mapped_column(ForeignKey("extractions.id"), index=True)
    findings: Mapped[list[Any]] = mapped_column(default=list)
    tool_trace: Mapped[list[Any]] = mapped_column(default=list)
    counterparty_id: Mapped[str | None] = mapped_column(String(32), default=None)
    counterparty_score: Mapped[float | None] = mapped_column(Float, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Decision(Base):
    """Stage 06 output. Produced by deterministic rules, never by the model."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    enrichment_id: Mapped[int] = mapped_column(ForeignKey("enrichments.id"), index=True)
    route: Mapped[Route] = mapped_column(String(16), index=True)
    reasons: Mapped[list[Any]] = mapped_column(default=list)
    blocking_fields: Mapped[list[Any]] = mapped_column(default=list)
    rules_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Contract(Base):
    """Stage 07 output for the clean path: the record downstream systems read."""

    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"), unique=True)
    counterparty_id: Mapped[str | None] = mapped_column(String(32), default=None)
    counterparty_name: Mapped[str] = mapped_column(String(512), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ReviewItem(Base):
    """Stage 07 output for the uncertain path: the human queue."""

    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"), unique=True)
    state: Mapped[str] = mapped_column(String(16), default="open", index=True)
    human_corrections: Mapped[dict[str, Any]] = mapped_column(default=dict)
    resolved_by: Mapped[str | None] = mapped_column(String(128), default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LLMCall(Base):
    """The cost ledger. Written by llm.client on every call, success or failure."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id"), default=None, index=True
    )
    purpose: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(64))
    effort: Mapped[str] = mapped_column(String(16), default="")

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)

    usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    stop_reason: Mapped[str | None] = mapped_column(String(32), default=None)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class DeadLetter(Base):
    """Anything the pipeline could not finish, with enough context to replay it."""

    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(primary_key=True)
    attachment_id: Mapped[int] = mapped_column(ForeignKey("attachments.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    error_class: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
