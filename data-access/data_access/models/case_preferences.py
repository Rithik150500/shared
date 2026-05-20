"""Per-user-per-case Munshi notification preferences.

Layered on top of the shared ``cases`` table. Composite PK matches Case's
natural query pattern (user_id, cnr). Separate table — not extra columns
on Case — so Case remains brand-neutral metadata and Nowlez can layer
its own per-case prefs without conflict.

Schema mirrors what bot_scaffold.SavedCase used to carry (alert_level,
snooze_until, digest_enabled) plus audit timestamps. Created by sub-project
A's completion (2026-05-20) to close the read-side gap left when save_case
migrated to data_access.Case (sub-project A Step 4) but read-side handlers
continued querying bot_scaffold.SavedCase.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# SQLite-compat variant. See user.py / case.py / billing.py for rationale.
UUIDType = UUID(as_uuid=True).with_variant(String(36), "sqlite")


class CasePreferences(Base):
    """Per-user-per-case notification preferences (Munshi-driven, shared schema)."""

    __tablename__ = "case_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    cnr: Mapped[str] = mapped_column(String(16), primary_key=True)
    alert_level: Mapped[str] = mapped_column(
        Text, nullable=False, default="all", server_default="all",
    )
    snooze_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    digest_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "alert_level IN ('all', 'orders_only', 'hearings_only', 'digest_only')",
            name="case_preferences_alert_level_check",
        ),
        Index("case_preferences_user_id_idx", "user_id"),
        # implicit_returning=False dodges a SQLAlchemy 2.0 bug: batched
        # INSERTs against tables with a (UUID, ...) composite PK on SQLite
        # cause sentinel-matching failures because the dialect can't reliably
        # map the RETURNING-clause rows back to the original parameter dicts.
        # Without this flag, two .add() calls in one .flush() raise
        # InvalidRequestError on commit. data_access.models.upsell sets the
        # same flag for the same reason (audit fix A-11: cleaned up stale
        # reference to the now-deleted bot_scaffold.SavedCase model).
        {"implicit_returning": False},
    )
