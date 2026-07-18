from __future__ import annotations

import uuid
from datetime import date, datetime

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
    # Sub-project E (migration 20260601_subproject_e_billing): postpaid
    # anniversary day-of-month used by `case_billing.munshi.cycles`.
    # Nullable here for SQLite test compatibility (the migration backfills
    # then ALTERs to NOT NULL on Postgres); tests that exercise the column
    # always populate it.
    billing_anniversary_date: Mapped[date | None] = mapped_column(
        Date, nullable=True,
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

    # Sub-project E migration 20260601_subproject_e_billing drops NOT NULL on
    # tier so the "trial / no tier picked" state can be represented as NULL.
    # The check constraint there allows NULL or one of
    # ('advocate','counsel','chambers','free').
    tier: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    tier_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Sub-project E: 30-day Chambers trial state. NULL when the user has
    # never been in a Nowlez trial (e.g. Munshi-only signup); set at
    # `create_trial_for_new_signup` time.
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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

    # Sub-project B: per-user WhatsApp consent flags (opt-out defaults TRUE).
    # The STOP-keyword handler flips both to False; the Nowlez settings UI
    # toggles them independently.
    whatsapp_events_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    whatsapp_reminders_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # Sub-project C amendment to sub-project D: per-user dismissal of the
    # post-merge welcome banner shown to users whose Munshi account merged
    # into a Nowlez login. Boolean (default FALSE) + nullable timestamp so
    # we can audit when the banner was dismissed.
    merge_banner_dismissed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    merge_banner_dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Unified-auth D4: "verified email on account" signal. True on the first
    # successful email-OTP verify against this users.id and on the reviewed
    # Sub-A/Sub-G email backfill. Chosen over a new users column so the shared
    # core table is untouched and the bot does not need this flag.
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # Sub-project G: forensic-only column recording the SQLite 8-char id
    # for each user that existed pre-G. NO FK references this column;
    # production code uses the Postgres UUID exclusively. Used by support
    # staff when resolving tickets that reference old short-UUIDs from
    # email histories or audit logs. Droppable ~6 months post-G.
    legacy_sqlite_id: Mapped[str | None] = mapped_column(
        Text, nullable=True, unique=True, index=True
    )

    # Supabase Auth: maps this user to their Supabase ``auth.users.id`` (the
    # JWT ``sub``), resolved once at the auth boundary. Supabase sits in FRONT
    # of this table — ``users.id`` remains the ownership spine — so there is
    # deliberately NO FK here, exactly as with legacy_sqlite_id above.
    #
    # The unique index is declared in __table_args__ rather than via
    # ``unique=True, index=True`` on the column so the model's predicate
    # matches the partial index the migration actually creates. Doing it the
    # legacy_sqlite_id way declares a NON-partial index under the same name,
    # which leaves --autogenerate permanently trying to drop and recreate it.
    supabase_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, nullable=True
    )

    # Sub-G step 1: orphan columns migrated from the SQLite `users` table —
    # identity-channel users need a PG home for these (no SQLite row).
    monthly_upload_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    usage_reset_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_export_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_case_exports_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    unsubscribed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_case_email_sent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_digest_sent_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "users_nowlez_referral_code_idx",
            "referral_code",
            postgresql_where=text("referral_code IS NOT NULL"),
        ),
        Index(
            "ix_users_nowlez_supabase_user_id",
            "supabase_user_id",
            unique=True,
            postgresql_where=text("supabase_user_id IS NOT NULL"),
        ),
    )
