"""g: add notifications, push_subscriptions, user_drip_state (notifications cohort)

Revision ID: 20260612_g_notifications
Revises: 20260611_g_content
Create Date: 2026-05-28

notifications is client-scoped (forensic legacy_sqlite_id idempotency key);
push_subscriptions is user-scoped with UNIQUE(user_id, endpoint); user_drip_state
is one row per user (PK = user_id). Chains after 20260611_g_content.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260612_g_notifications"
down_revision = "20260611_g_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_cnr", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("is_read", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("legacy_sqlite_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("notifications_client_idx", "notifications", ["client_id"])
    op.create_index(
        "ix_notifications_legacy_sqlite_id", "notifications", ["legacy_sqlite_id"],
        unique=True, postgresql_where=sa.text("legacy_sqlite_id IS NOT NULL"),
    )

    op.create_table(
        "push_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "endpoint", name="push_subscriptions_user_endpoint_uq"),
    )
    op.create_index("push_subscriptions_user_idx", "push_subscriptions", ["user_id"])

    op.create_table(
        "user_drip_state",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("track_today", sa.Text(), nullable=True),
        sa.Column("became_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_step_sent_day", sa.Integer(), server_default=sa.text("-1"), nullable=False),
        sa.Column("catch_up_pending", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("catch_up_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_drip_state")
    op.drop_index("push_subscriptions_user_idx", "push_subscriptions")
    op.drop_table("push_subscriptions")
    op.drop_index("ix_notifications_legacy_sqlite_id", "notifications")
    op.drop_index("notifications_client_idx", "notifications")
    op.drop_table("notifications")
