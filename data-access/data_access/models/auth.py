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

# SQLite-compatibility variants: Postgres types with String fallbacks for sqlite.
# Lets consumers (e.g. Munshi tests) use in-memory SQLite for Base.metadata.create_all().
UUIDType = UUID(as_uuid=True).with_variant(String(36), "sqlite")
INETType = INET().with_variant(String(45), "sqlite")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
        # NOTE: server_default=text("gen_random_uuid()") is omitted here because
        # SQLite errors on the literal at CREATE TABLE time. Prod schema is
        # preserved via the Alembic baseline migration (op.create_table sets
        # server_default explicitly). Python-side default=uuid.uuid4 covers ORM
        # INSERTs on both Postgres and SQLite.
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
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
    ip_address: Mapped[str | None] = mapped_column(INETType, nullable=True)
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
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
        # NOTE: server_default=text("gen_random_uuid()") is omitted here because
        # SQLite errors on the literal at CREATE TABLE time. Prod schema is
        # preserved via the Alembic baseline migration (op.create_table sets
        # server_default explicitly). Python-side default=uuid.uuid4 covers ORM
        # INSERTs on both Postgres and SQLite.
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
    ip_address: Mapped[str | None] = mapped_column(INETType, nullable=True)

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


class LoginRequest(Base):
    __tablename__ = "login_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
        # NOTE: server_default=text("gen_random_uuid()") is omitted here so
        # SQLite Base.metadata.create_all() works in tests; the Alembic
        # migration sets gen_random_uuid() on the Postgres path only.
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending"
    )
    brand: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    poll_bind_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INETType, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "direction IN ('web2bot', 'bot2web')",
            name="login_requests_direction_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'consumed', 'expired')",
            name="login_requests_status_check",
        ),
        CheckConstraint(
            "brand IN ('munshi', 'nowlez')",
            name="login_requests_brand_check",
        ),
        Index("login_requests_token_hash_idx", "token_hash"),
        # Partial index covers confirmed-but-stale rows too (must be swept),
        # not pending-only. Postgres rejects volatile NOW() in index predicates,
        # so the predicate filters on status only.
        Index(
            "login_requests_expires_at_idx",
            "expires_at",
            postgresql_where=text("status IN ('pending', 'confirmed')"),
        ),
        Index("login_requests_ip_rate_idx", "ip_address", "created_at"),
    )


class EmailOtpCode(Base):
    __tablename__ = "email_otp_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
        # NOTE: gen_random_uuid() omitted in the model (SQLite create_all);
        # set on the Postgres migration path only.
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
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
    ip_address: Mapped[str | None] = mapped_column(INETType, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "delivery_status IN ('pending', 'delivered', 'failed')",
            name="email_otp_delivery_status_check",
        ),
        # Postgres rejects volatile NOW() in index predicates; filter on used_at only.
        Index(
            "email_otp_email_active_idx",
            "email",
            "created_at",
            postgresql_where=text("used_at IS NULL"),
        ),
        Index("email_otp_email_rate_idx", "email", "created_at"),
        Index(
            "email_otp_expires_idx",
            "expires_at",
            postgresql_where=text("used_at IS NULL"),
        ),
    )
