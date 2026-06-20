"""g step 1: add 7 orphan users columns to users_nowlez

Revision ID: 20260620_g_orphan_cols
Revises: 20260619_unified_login
Create Date: 2026-06-20

Sub-G cutover step 1. These 7 columns lived only on the SQLite `users` table;
identity-channel (UUID) users have no SQLite row, so they need a Postgres home.
All nullable / defaulted so the migration is safe on the populated prod table.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260620_g_orphan_cols"
down_revision = "20260619_unified_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users_nowlez", sa.Column(
        "monthly_upload_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users_nowlez", sa.Column(
        "usage_reset_date", sa.Date(), nullable=True))
    op.add_column("users_nowlez", sa.Column(
        "last_export_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users_nowlez", sa.Column(
        "last_case_exports_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users_nowlez", sa.Column(
        "unsubscribed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users_nowlez", sa.Column(
        "first_case_email_sent", sa.Boolean(), nullable=False,
        server_default=sa.text("false")))
    op.add_column("users_nowlez", sa.Column(
        "last_digest_sent_date", sa.Date(), nullable=True))


def downgrade() -> None:
    for col in (
        "last_digest_sent_date", "first_case_email_sent", "unsubscribed_at",
        "last_case_exports_at", "last_export_at", "usage_reset_date",
        "monthly_upload_count",
    ):
        op.drop_column("users_nowlez", col)
