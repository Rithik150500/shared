"""WhatsApp idempotency + delivery-log models, shared by Munshi and Nowlez.

Both tables live in the shared schema so cross-brand idempotency and delivery
tracking flow through one DB. The classes follow the same SQLite-compatibility
pattern as the rest of `data_access.models`: ``UUID(as_uuid=True).with_variant``
+ ``default=uuid.uuid4`` so ``Base.metadata.create_all()`` against ``:memory:``
SQLite works for fast unit tests.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# Mirror the with_variant() pattern used by data_access/models/user.py — keeps
# DDL portable between Postgres (production) and in-memory SQLite (unit tests).
UUIDType = UUID(as_uuid=True).with_variant(String(36), "sqlite")


class MessageLog(Base):
    """Idempotency table for inbound webhook deduplication.

    Meta retries any un-ack'd webhook for ~24h. We INSERT ON CONFLICT DO NOTHING
    keyed on (meta_message_id) so each inbound dispatches exactly once. The
    table is shared across Munshi and Nowlez so cross-brand inbound dedup
    flows through the same row.

    Carries the legacy Munshi columns (``user_id``, ``processed_at``,
    ``handler_name``, ``error``) so Munshi's existing app.py / idempotency.py
    keep working unchanged after the model is relocated here. ``user_phone``
    is non-null for parity with Nowlez's claim_message; ``user_id`` is
    nullable because the inbound webhook can resolve the user id only after
    the row is claimed.
    """

    __tablename__ = "message_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
        # server_default omitted: SQLite errors on gen_random_uuid() literal at
        # CREATE TABLE time. The Python-side default covers ORM INSERTs on both
        # dialects; production schema is set via the Alembic migration.
    )
    meta_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Phase 1 coexistence: both ``user_phone`` and ``user_id`` are present so
    # Munshi (which inserts user_id) and Nowlez (which inserts user_phone) can
    # share the same table without either having to refactor inserts first.
    user_phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    # Legacy Munshi columns, preserved so the existing app.py handler code
    # (which UPDATEs processed_at / handler_name / error after dispatch)
    # keeps working unchanged.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handler_name: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("meta_message_id", name="message_log_meta_id_unique"),
        Index("message_log_user_phone_idx", "user_phone"),
        Index("message_log_user_id_idx", "user_id"),
        Index("message_log_received_idx", "received_at"),
    )


class WhatsAppDeliveryLog(Base):
    """Per-send tracking: enqueue -> sent -> delivered -> read.

    One row per outbound send (template or text). The dispatch pipeline writes
    it as ``pending`` on enqueue, populates ``meta_message_id`` after the Cloud
    API ack, and the inbound webhook flips ``delivery_status`` + timestamps as
    Meta delivers the receipts.
    """

    __tablename__ = "whatsapp_delivery_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    template_name: Mapped[str] = mapped_column(Text, nullable=False)
    brand: Mapped[str] = mapped_column(Text, nullable=False)
    meta_message_id: Mapped[str | None] = mapped_column(Text)
    rq_job_id: Mapped[str | None] = mapped_column(Text)
    delivery_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending", default="pending",
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    related_case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("cases.id", ondelete="SET NULL"),
    )
    related_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("case_orders.id", ondelete="SET NULL"),
    )
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # B-3 dedup key: IST-localized date of the send. NULL for transactional
    # templates that may legitimately fire multiple times per user per day
    # (signup welcome, OTP, order-uploaded, etc.). Set by the worker via an
    # INSERT ... ON CONFLICT DO NOTHING claim for daily-cadence templates
    # (``nowlez_tomorrow_hearings_v1``, ``nowlez_weekly_summary_v1``) — the
    # partial unique index below makes the at-most-once-per-day guarantee
    # cross-pod serializable.
    send_date_ist: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "brand IN ('munshi', 'nowlez')",
            name="whatsapp_delivery_log_brand_check",
        ),
        CheckConstraint(
            "delivery_status IN ('pending', 'sent', 'delivered', 'read', 'failed')",
            name="whatsapp_delivery_log_status_check",
        ),
        Index("whatsapp_delivery_log_user_id_idx", "user_id", "enqueued_at"),
        Index(
            "whatsapp_delivery_log_status_idx", "delivery_status",
            postgresql_where="delivery_status IN ('pending', 'failed')",
        ),
        Index("whatsapp_delivery_log_meta_msg_idx", "meta_message_id"),
        # B-3: PARTIAL unique index — only rows with ``send_date_ist IS NOT
        # NULL`` participate. This lets daily-cadence sends share the table
        # with transactional sends that intentionally have no per-day key.
        # The same predicate is used on both Postgres (production) and
        # SQLite (tests) — modern SQLite (3.8+) supports partial indexes.
        Index(
            "whatsapp_delivery_log_user_template_day_unique",
            "user_id", "template_name", "send_date_ist",
            unique=True,
            postgresql_where=text("send_date_ist IS NOT NULL"),
            sqlite_where=text("send_date_ist IS NOT NULL"),
        ),
    )
