"""g: housekeeping cohort — audit_log.legacy_sqlite_id + feedback + waitlist

Revision ID: 20260613_g_housekeeping
Revises: 20260612_g_notifications
Create Date: 2026-05-28

Adds the forensic legacy_sqlite_id idempotency column to the existing audit_log
table, and creates the feedback + waitlist tables. (debug_traces, webhook_events
and court_hierarchy are intentionally NOT migrated — ephemeral / superseded /
re-seedable.) Chains after 20260612_g_notifications.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260613_g_housekeeping"
down_revision = "20260612_g_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("legacy_sqlite_id", sa.Text(), nullable=True))
    op.create_index(
        "ix_audit_log_legacy_sqlite_id", "audit_log", ["legacy_sqlite_id"],
        unique=True, postgresql_where=sa.text("legacy_sqlite_id IS NOT NULL"),
    )

    op.create_table(
        "feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("page_route", sa.Text(), nullable=True),
        sa.Column("legacy_sqlite_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("feedback_user_idx", "feedback", ["user_id"])
    op.create_index(
        "ix_feedback_legacy_sqlite_id", "feedback", ["legacy_sqlite_id"],
        unique=True, postgresql_where=sa.text("legacy_sqlite_id IS NOT NULL"),
    )

    op.create_table(
        "waitlist",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("practice_area", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="waitlist_email_uq"),
    )


def downgrade() -> None:
    op.drop_table("waitlist")
    op.drop_index("ix_feedback_legacy_sqlite_id", "feedback")
    op.drop_index("feedback_user_idx", "feedback")
    op.drop_table("feedback")
    op.drop_index("ix_audit_log_legacy_sqlite_id", "audit_log")
    op.drop_column("audit_log", "legacy_sqlite_id")
