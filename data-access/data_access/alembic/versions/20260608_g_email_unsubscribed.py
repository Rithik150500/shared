"""g: add users_nowlez.email_unsubscribed_at

Revision ID: 20260608_g_email_unsubscribed
Revises: 20260607_g_legacy_sqlite_id
Create Date: 2026-05-28

Sub-project G user cutover. Adds a nullable timestamptz column to users_nowlez
recording the legacy SQLite ``users.unsubscribed_at`` (global marketing-email
opt-out). Compliance-relevant consent state, so it must not be dropped during
the SQLite -> Postgres migration. Checked per-user by primary key, so no
secondary index is needed.

Chains after ``20260607_g_legacy_sqlite_id`` (the latest Sub-project G head);
``alembic upgrade heads`` applies it alongside any other open head.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260608_g_email_unsubscribed"
down_revision = "20260607_g_legacy_sqlite_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users_nowlez",
        sa.Column("email_unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users_nowlez", "email_unsubscribed_at")
