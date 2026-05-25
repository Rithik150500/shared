"""g: add users_nowlez.legacy_sqlite_id forensic column

Revision ID: 20260607_g_legacy_sqlite_id
Revises: 20260525_b5b_nowlez_hook_columns
Create Date: 2026-05-25

Sub-project G "ID translation: forensic-only column". Adds a nullable
TEXT column to users_nowlez recording each user's pre-G SQLite 8-char id.
No FK references this column; production code uses the Postgres UUID.
Unique partial index (NOT NULL only) so support-staff lookups by legacy
id are fast; pre-G users who never had a SQLite id are all NULL.

down_revision targets the current head per ``alembic heads`` on 2026-05-25
(``20260525_b5b_nowlez_hook_columns``). The 20260607 filename prefix
keeps the migration after the latest filename-dated migration in the
versions/ directory (``20260606_b3_dedup_send_per_day.py``) so the file
listing stays roughly chronological alongside the on-disk Create Date.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260607_g_legacy_sqlite_id"
down_revision = "20260525_b5b_nowlez_hook_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users_nowlez",
        sa.Column("legacy_sqlite_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_users_nowlez_legacy_sqlite_id",
        "users_nowlez",
        ["legacy_sqlite_id"],
        unique=True,
        postgresql_where=sa.text("legacy_sqlite_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_users_nowlez_legacy_sqlite_id", "users_nowlez")
    op.drop_column("users_nowlez", "legacy_sqlite_id")
