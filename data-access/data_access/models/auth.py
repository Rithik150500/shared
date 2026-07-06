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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .case import UUIDType as _RoundtripSafeUUIDType

# SQLite-compatibility variants: Postgres types with String fallbacks for sqlite.
# Lets consumers (e.g. Munshi tests) use in-memory SQLite for Base.metadata.create_all().
#
# NOTE: this plain with_variant() form only adjusts DDL — on a fresh SQLite read
# (not served from the session identity map) the driver hands back a str, not a
# uuid.UUID, which breaks identity-map matching for round-tripped rows (see
# case.py's _UUIDType docstring for the full story). Existing classes below
# still use this legacy form (unchanged, out of scope here); UserIdentity below
# uses the roundtrip-safe `_RoundtripSafeUUIDType` instead.
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
    # Rotation lineage: every refresh issues a new row sharing the original
    # login's family_id, so a detected token-reuse can revoke the whole family.
    # Defaults to a fresh uuid per row (a new login starts its own family); the
    # rotation path passes the parent's family_id explicitly.
    family_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, nullable=False, default=uuid.uuid4
    )
    # Successor pointer set when this row is rotated away. NULL on a live row and
    # on an explicitly-revoked (logout / password-change) row — that NULL is how
    # reuse-detection distinguishes a rotated token (replay = theft) from a
    # logged-out one (replay = plain invalid, no alarm). Deliberately NOT a FK:
    # a self-referential CASCADE on a users.id-cascading table is needless
    # complexity for an internal pointer we control (cf. legacy_sqlite_id).
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
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
        # Family-revoke (reuse-detection) lookups by family_id.
        Index("auth_sessions_family_id_idx", "family_id"),
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
        # token_hash equality lookups are served by the implicit unique index
        # created by the column's unique=True constraint — a separate index here
        # would be redundant (double write-amplification on Postgres).
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


class UserExternalIdentity(Base):
    """Federated (OAuth/OIDC) identity linked to a core ``users`` row.

    A dedicated table (rather than a ``users_nowlez.google_sub`` column) mirrors
    how the repo separates auth artifacts (otp_codes / email_otp_codes /
    login_requests) and generalizes to additional providers later.

    The provider ``sub`` (Google's stable subject id) is the authoritative
    anchor — emails can change/be reassigned, the ``sub`` does not — so login
    resolves on ``(provider, provider_sub)`` first and falls back to the verified
    email. ``email`` here is the provider-asserted address at link time, kept for
    audit/support only; ``users.email`` remains the canonical identity email.
    """

    __tablename__ = "user_external_identities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
        # gen_random_uuid() omitted in the model (SQLite create_all chokes on the
        # literal); set on the Postgres migration path only — same as every other
        # model/migration pair in this package.
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_sub: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "provider IN ('google')",
            name="user_external_identities_provider_check",
        ),
        # One account per (provider, sub) — the stable login anchor.
        UniqueConstraint(
            "provider", "provider_sub", name="user_external_identities_provider_sub_key"
        ),
        # At most one identity per provider per user (a user links one Google acct).
        UniqueConstraint(
            "user_id", "provider", name="user_external_identities_user_provider_key"
        ),
        Index("user_external_identities_user_id_idx", "user_id"),
    )


class UserIdentity(Base):
    """A phone/email identity that routes to a core ``users`` row.

    Phase-1 home for *alias* identities (a second phone recognised by the bot,
    a second email that can OTP-login) and the intended future superset table
    that ``user_external_identities`` (OAuth) folds into. A value belongs to at
    most one account (``UNIQUE(kind, value)``); only ``verified_at IS NOT NULL``
    rows route/authenticate. The primary identifiers stay on ``users.phone`` /
    ``users.email`` — an alias value is never also a live primary elsewhere
    (``identity_alias_dao.add_alias`` reclaims/refuses on collision).
    """

    __tablename__ = "user_identities"

    id: Mapped[uuid.UUID] = mapped_column(
        _RoundtripSafeUUIDType,
        primary_key=True,
        default=uuid.uuid4,
        # gen_random_uuid() omitted in the model (SQLite create_all chokes on the
        # literal); set on the Postgres migration path only.
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        _RoundtripSafeUUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    added_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("kind IN ('phone', 'email')", name="user_identities_kind_check"),
        UniqueConstraint("kind", "value", name="user_identities_kind_value_key"),
        Index("user_identities_user_id_idx", "user_id"),
    )
