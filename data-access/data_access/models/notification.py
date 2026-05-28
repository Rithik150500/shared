from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# Postgres UUID with a String(36) fallback for SQLite (tests). See models/user.py.
UUIDType = UUID(as_uuid=True).with_variant(String(36), "sqlite")


class Notification(Base):
    """In-app notification, scoped to a client. Migrated from SQLite in
    Sub-project G (notifications cohort)."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    case_cnr: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    legacy_sqlite_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("notifications_client_idx", "client_id"),)


class PushSubscription(Base):
    """Web-push subscription for a user. UNIQUE(user_id, endpoint) is both the
    SQLite constraint and the migration idempotency key."""

    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "endpoint", name="push_subscriptions_user_endpoint_uq"),
        Index("push_subscriptions_user_idx", "user_id"),
    )


class UserDripState(Base):
    """Lifecycle-drip cursor, one row per user (PK = user_id). Migrated from
    SQLite ``user_drip_state``."""

    __tablename__ = "user_drip_state"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    track_today: Mapped[str | None] = mapped_column(Text, nullable=True)
    became_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_step_sent_day: Mapped[int] = mapped_column(Integer, nullable=False, default=-1, server_default="-1")
    catch_up_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    catch_up_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
