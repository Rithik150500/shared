"""baseline: identity tables (users, users_munshi, users_nowlez, auth_sessions, otp_codes, audit_log)

Revision ID: 20260515_0001
Revises:
Create Date: 2026-05-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260515_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("phone", sa.String(20), unique=True, nullable=True),
        sa.Column("email", sa.String(254), unique=True, nullable=True),
        sa.Column("password_hash", sa.Text, nullable=True),
        sa.Column("locale", sa.String(8), nullable=False, server_default="en"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("users_phone_idx", "users", ["phone"], postgresql_where=sa.text("phone IS NOT NULL"))
    op.create_index("users_email_idx", "users", ["email"], postgresql_where=sa.text("email IS NOT NULL"))

    # users_munshi
    op.create_table(
        "users_munshi",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("current_state", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("onboarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("re_engage_opted_out", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("re_engage_snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tutorial_tips_seen", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("reset_re_engage_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # users_nowlez
    op.create_table(
        "users_nowlez",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("is_admin", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("tier", sa.Text, nullable=False, server_default="free"),
        sa.Column("tier_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("monthly_chat_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("daily_chat_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("daily_chat_date", sa.Date, nullable=True),
        sa.Column("monthly_draft_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("monthly_order_pages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("monthly_doc_pages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("monthly_total_pages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("onboarding_nudge_sent", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("last_digest_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("feature_highlight_sent", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("trial_warning_sent", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("trial_expired_sent", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("win_back_sent", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("referral_code", sa.Text, unique=True, nullable=True),
        sa.Column("referred_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("razorpay_customer_id", sa.Text, nullable=True),
        sa.Column("razorpay_subscription_id", sa.Text, nullable=True),
        sa.Column("onboarding_state", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "users_nowlez_referral_code_idx", "users_nowlez", ["referral_code"],
        postgresql_where=sa.text("referral_code IS NOT NULL"),
    )

    # auth_sessions
    op.create_table(
        "auth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("refresh_token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("ip_address", postgresql.INET, nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("auth_sessions_user_id_idx", "auth_sessions", ["user_id"], postgresql_where=sa.text("revoked_at IS NULL"))
    op.create_index("auth_sessions_expires_at_idx", "auth_sessions", ["expires_at"], postgresql_where=sa.text("revoked_at IS NULL"))

    # otp_codes
    op.create_table(
        "otp_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("code_hash", sa.Text, nullable=False),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("delivery_status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("delivery_provider_id", sa.Text, nullable=True),
        sa.Column("attempts_remaining", sa.SmallInteger, nullable=False, server_default="3"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", postgresql.INET, nullable=True),
        sa.CheckConstraint("channel IN ('whatsapp', 'sms')", name="otp_channel_check"),
        sa.CheckConstraint("delivery_status IN ('pending', 'delivered', 'failed')", name="otp_delivery_status_check"),
    )
    # NOTE: original spec used `expires_at > NOW()` in the predicate, but Postgres
    # rejects volatile functions (NOW()) in index predicates. Query-time filtering on
    # expires_at is still supported by otp_codes_expires_at_idx below.
    op.create_index(
        "otp_codes_phone_active_idx", "otp_codes",
        ["phone", sa.text("created_at DESC")],
        postgresql_where=sa.text("used_at IS NULL"),
    )
    op.create_index("otp_codes_phone_rate_limit_idx", "otp_codes", ["phone", sa.text("created_at DESC")])
    op.create_index("otp_codes_expires_at_idx", "otp_codes", ["expires_at"], postgresql_where=sa.text("used_at IS NULL"))

    # audit_log
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("ip_address", postgresql.INET, nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("source IN ('munshi', 'nowlez', 'identity', 'system')", name="audit_source_check"),
    )
    op.create_index("audit_log_created_at_idx", "audit_log", [sa.text("created_at DESC")])
    op.create_index(
        "audit_log_user_id_idx", "audit_log",
        ["user_id", sa.text("created_at DESC")],
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index("audit_log_event_type_idx", "audit_log", ["event_type", sa.text("created_at DESC")])


def downgrade():
    op.drop_table("audit_log")
    op.drop_table("otp_codes")
    op.drop_table("auth_sessions")
    op.drop_table("users_nowlez")
    op.drop_table("users_munshi")
    op.drop_table("users")
    # leave pgcrypto installed
