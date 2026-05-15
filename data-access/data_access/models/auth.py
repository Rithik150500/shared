from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    refresh_token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "auth_sessions_user_id_idx",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "auth_sessions_expires_at_idx",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class OtpCode(Base):
    __tablename__ = "otp_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending"
    )
    delivery_provider_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts_remaining: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=3, server_default="3"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)

    __table_args__ = (
        CheckConstraint("channel IN ('whatsapp', 'sms')", name="otp_channel_check"),
        CheckConstraint(
            "delivery_status IN ('pending', 'delivered', 'failed')",
            name="otp_delivery_status_check",
        ),
        # NOTE: Postgres rejects volatile functions (NOW()) in index predicates,
        # so this partial index only filters on used_at IS NULL. Callers that need
        # to exclude expired rows should add `AND expires_at > NOW()` to their query.
        Index(
            "otp_codes_phone_active_idx",
            "phone",
            "created_at",
            postgresql_where=text("used_at IS NULL"),
        ),
        Index("otp_codes_phone_rate_limit_idx", "phone", "created_at"),
        Index(
            "otp_codes_expires_at_idx",
            "expires_at",
            postgresql_where=text("used_at IS NULL"),
        ),
    )
