"""Munshi upsell event tracking.

One row per (user_id, stage). UNIQUE constraint enforces "each stage sent
at most once per user" — the cron's stage-determination logic depends on
this for idempotency. converted_at + converted_to_tier are filled in by
the subscription.activated webhook handler (case_billing.shared.webhook_router
via case_billing.nowlez.upsell.record_upgrade_conversion).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# Match the project convention used in case_preferences: PostgresUUID with
# String(36) variant for SQLite.
UUIDType = PostgresUUID(as_uuid=True).with_variant(String(36), "sqlite")


class MunshiUpsellEvent(Base):
    """Per-user-per-stage WhatsApp upsell send log (sub-project C).

    UNIQUE(user_id, stage) enforces "each stage at most once per user".
    The daily upsell cron relies on this for idempotency: if a worker
    races itself, the second insert raises IntegrityError and the cron
    retries cleanly without sending a duplicate template.

    Conversion fields (converted_at, converted_to_tier) are filled in by
    the Razorpay subscription.activated webhook handler when the user
    upgrades to Nowlez — wires the upsell prompt to its outcome for
    analytics.
    """

    __tablename__ = "munshi_upsell_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_reason: Mapped[str] = mapped_column(Text, nullable=False)
    case_count_at_send: Mapped[int] = mapped_column(Integer, nullable=False)
    spend_at_send_rupees: Mapped[int] = mapped_column(BigInteger, nullable=False)
    template_name: Mapped[str] = mapped_column(Text, nullable=False)
    meta_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    converted_to_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "stage", name="munshi_upsell_events_unique_stage",
        ),
        CheckConstraint(
            "stage IN ('initial', 'reminder', 'final')",
            name="munshi_upsell_events_stage_check",
        ),
        CheckConstraint(
            "trigger_reason IN ('case_count', 'spend', 'both')",
            name="munshi_upsell_events_trigger_check",
        ),
        # ORM-side index uses text("sent_at DESC") to mirror the Postgres
        # migration; SQLite ignores DESC in B-tree but the index still
        # exists for plan compatibility.
        Index(
            "munshi_upsell_events_user_id_idx",
            "user_id",
            "sent_at",
        ),
        # implicit_returning=False dodges SQLAlchemy 2.0's batched
        # INSERT...RETURNING sentinel-matching bug on sqlite for UUID PK
        # tables — same fix CasePreferences uses (see case_preferences.py
        # rationale comment). Without this, two record_upsell_event calls
        # in one flush raise InvalidRequestError on commit.
        {"implicit_returning": False},
    )
