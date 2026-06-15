"""Phone-keyed broadcast ledger + suppression models.

``wa_digits`` is an E.164 phone number WITHOUT the leading ``+``
(e.g. ``919643460175``). These tables are intentionally decoupled from
``users.id`` because broadcast recipients are raw phone numbers, not
necessarily registered users.

- ``WaSuppression`` — deny list consulted before every broadcast send
  (opt-out / undeliverable / manual / block). UNIQUE on ``wa_digits``.
- ``WaBroadcastLog`` — durable exactly-once sent-ledger. UNIQUE on
  ``(campaign, wa_digits)`` so a resume/retry loop never double-sends.
  Also captures Meta's numeric ``error_code`` for analytics.

Both tables follow the same portability pattern as ``data_access/models/whatsapp.py``:
``UUIDType = UUID(as_uuid=True).with_variant(String(36), "sqlite")`` so
``Base.metadata.create_all()`` against ``:memory:`` SQLite works for fast
unit tests.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .whatsapp import UUIDType


class WaSuppression(Base):
    """Deny list for broadcast sends.

    A phone number in this table is suppressed — the broadcast driver will
    skip it without sending. ``reason`` is one of: ``stop``,
    ``undeliverable``, ``manual``, ``block``. ``source`` is a free-form
    label for the process that triggered the suppression (e.g. ``webhook``,
    ``import``, ``support``).

    UNIQUE on ``wa_digits`` so ``suppress()`` is idempotent via
    INSERT ON CONFLICT DO NOTHING.
    """

    __tablename__ = "wa_suppression"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, primary_key=True, default=uuid.uuid4
    )
    wa_digits: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("wa_digits", name="wa_suppression_wa_digits_unique"),
    )


class WaBroadcastLog(Base):
    """Per-send tracking for broadcast campaigns.

    One row per (campaign, phone). The broadcast driver writes ``pending``
    on claim, updates ``meta_message_id`` + status after the Meta Cloud API
    call, and the inbound webhook flips ``status`` + timestamps as Meta
    delivers receipts.

    ``error_code`` holds Meta's numeric error code (e.g. 131026 = re-
    engagement window) for analytics / retry decisions.

    UNIQUE on ``(campaign, wa_digits)`` — ``claim_send()`` is the
    exactly-once gate via INSERT ON CONFLICT DO NOTHING.
    """

    __tablename__ = "wa_broadcast_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, primary_key=True, default=uuid.uuid4
    )
    campaign: Mapped[str] = mapped_column(Text, nullable=False)
    wa_digits: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str | None] = mapped_column(Text)
    template_name: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    meta_message_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending", default="pending"
    )
    error_code: Mapped[int | None] = mapped_column(Integer)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "campaign",
            "wa_digits",
            name="wa_broadcast_log_campaign_phone_unique",
        ),
        Index("wa_broadcast_log_wamid_idx", "meta_message_id"),
        Index("wa_broadcast_log_campaign_status_idx", "campaign", "status"),
    )
