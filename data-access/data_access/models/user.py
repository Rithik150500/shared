from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# SQLite-compatibility variants: Postgres types with generic fallbacks for sqlite.
# Lets consumers (e.g. Munshi tests) use in-memory SQLite for Base.metadata.create_all().
UUIDType = UUID(as_uuid=True).with_variant(String(36), "sqlite")
JSONBType = JSONB().with_variant(JSON(), "sqlite")


class User(Base):
    __tablename__ = "users"

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
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="en", server_default="en")
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Kolkata", server_default="Asia/Kolkata"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("users_phone_idx", "phone", postgresql_where=text("phone IS NOT NULL")),
        Index("users_email_idx", "email", postgresql_where=text("email IS NOT NULL")),
    )


class UserMunshi(Base):
    __tablename__ = "users_munshi"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    current_state: Mapped[dict] = mapped_column(
        JSONBType,
        nullable=False,
        default=dict,
        # server_default omitted: Postgres `'{}'::jsonb` cast syntax fails on
        # SQLite. Prod schema preserves server_default via Alembic baseline.
        # Python-side default=dict covers ORM INSERTs on both dialects.
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    re_engage_opted_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    re_engage_snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tutorial_tips_seen: Mapped[list] = mapped_column(
        JSONBType,
        nullable=False,
        default=list,
        # server_default omitted: Postgres `'[]'::jsonb` cast syntax fails on
        # SQLite. Prod schema preserves server_default via Alembic baseline.
        # Python-side default=list covers ORM INSERTs on both dialects.
    )
    reset_re_engage_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserNowlez(Base):
    __tablename__ = "users_nowlez"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))

    tier: Mapped[str] = mapped_column(Text, nullable=False, default="free", server_default="free")
    tier_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    monthly_chat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    daily_chat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    daily_chat_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    monthly_draft_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    monthly_order_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    monthly_doc_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    monthly_total_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    onboarding_nudge_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    last_digest_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    feature_highlight_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    trial_warning_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    trial_expired_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    win_back_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))

    referral_code: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    referred_by: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    razorpay_customer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    razorpay_subscription_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    onboarding_state: Mapped[dict] = mapped_column(
        JSONBType,
        nullable=False,
        default=dict,
        # server_default omitted: Postgres `'{}'::jsonb` cast syntax fails on
        # SQLite. Prod schema preserves server_default via Alembic baseline.
        # Python-side default=dict covers ORM INSERTs on both dialects.
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "users_nowlez_referral_code_idx",
            "referral_code",
            postgresql_where=text("referral_code IS NOT NULL"),
        ),
    )
