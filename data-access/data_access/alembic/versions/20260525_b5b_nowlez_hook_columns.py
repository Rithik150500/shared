"""add nowlez hook migration columns (Nowlez PR 6 Phase B / B.5b)

Revision ID: 20260525_b5b_nowlez_hook_columns
Revises: 20260523_a_completion_cases_tsvector
Create Date: 2026-05-25

Adds Postgres-side columns required by Nowlez PR 6 Phase B (Sub-A cutover),
which migrates 3 idempotency hooks that currently live in the legacy SQLite
schema over to the shared Postgres `cases` / `case_orders_nowlez` tables:

- ``cases.first_ndoh_email_sent_at`` — set when the E2 "first NDOH email"
  notification is dispatched for a case. Used by the scheduler / hook
  pipeline to guarantee a single first-NDOH email per case across worker
  restarts and pod failovers.
- ``case_orders_nowlez.user_notified_at`` — set when the E3 "order
  user-notified" message is delivered. Replaces the legacy
  ``case_orders.user_notified_at`` SQLite column.

Both columns are nullable timezone-aware datetimes following the project
convention (e.g. ``cases.last_change_at``).  No backfill is required —
existing rows correctly have NULL ("never sent") and the Nowlez hook code
treats NULL as "not yet notified".

The third SQLite hook (toggle_refresh) maps onto the existing
``cases.refresh_enabled`` column, so this migration only adds the two
timestamp columns above; the DAO-level work lives in
``case_dao.toggle_refresh`` in the same commit.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260525_b5b_nowlez_hook_columns"
down_revision = "20260523_a_completion_cases_tsvector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column(
            "first_ndoh_email_sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "case_orders_nowlez",
        sa.Column(
            "user_notified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("case_orders_nowlez", "user_notified_at")
    op.drop_column("cases", "first_ndoh_email_sent_at")
