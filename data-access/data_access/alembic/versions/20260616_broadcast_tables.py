"""Broadcast ledger: wa_suppression + wa_broadcast_log tables.

Revision ID: 20260616_broadcast_tables
Revises: 20260607_g_legacy_sqlite_id
Create Date: 2026-06-16

Phase 3 of the Munshi broadcast send tooling (Tasks 2+3).

Creates two phone-keyed tables that are intentionally decoupled from the
existing ``whatsapp_delivery_log`` (which has a NOT NULL FK to ``users.id``
and cannot accommodate raw-phone-number broadcast recipients):

wa_suppression
  Deny list consulted before every broadcast send. A single row per
  ``wa_digits`` (UNIQUE) covers opt-out / undeliverable / manual / block
  reasons. The ``suppress()`` DAO uses INSERT ON CONFLICT DO NOTHING so
  repeated calls are idempotent and the first written ``reason`` wins.

wa_broadcast_log
  Durable exactly-once sent-ledger. UNIQUE on ``(campaign, wa_digits)``
  so the ``claim_send()`` DAO gate via INSERT ON CONFLICT DO NOTHING
  is serializable across concurrent workers and is safe to retry/resume.
  Also captures Meta's numeric ``error_code`` (e.g. 131026 = re-engagement
  window) for analytics and retry decisions.

``wa_digits`` = E.164 phone number WITHOUT the leading ``+``
(e.g. ``919643460175``).

Three indexes on ``wa_broadcast_log``:
- ``wa_broadcast_log_wamid_idx`` on ``meta_message_id`` — for inbound-
  webhook O(1) status updates via ``apply_broadcast_status()``.
- ``wa_broadcast_log_campaign_status_idx`` on ``(campaign, status)`` —
  for the driver's pending/sent cohort queries.
(The UNIQUE constraint on ``(campaign, wa_digits)`` is covered by its
own implicit index.)

``down_revision`` chains onto ``20260607_g_legacy_sqlite_id``, which is
the current head per ``alembic heads`` on 2026-06-16.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260616_broadcast_tables"
down_revision = "20260607_g_legacy_sqlite_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # wa_suppression — phone-keyed deny list
    # -----------------------------------------------------------------------
    op.create_table(
        "wa_suppression",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("wa_digits", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("wa_digits", name="wa_suppression_wa_digits_unique"),
    )

    # -----------------------------------------------------------------------
    # wa_broadcast_log — exactly-once sent-ledger
    # -----------------------------------------------------------------------
    op.create_table(
        "wa_broadcast_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign", sa.Text(), nullable=False),
        sa.Column("wa_digits", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=True),
        sa.Column("template_name", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("meta_message_id", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "campaign",
            "wa_digits",
            name="wa_broadcast_log_campaign_phone_unique",
        ),
    )

    op.create_index(
        "wa_broadcast_log_wamid_idx",
        "wa_broadcast_log",
        ["meta_message_id"],
    )
    op.create_index(
        "wa_broadcast_log_campaign_status_idx",
        "wa_broadcast_log",
        ["campaign", "status"],
    )


def downgrade() -> None:
    op.drop_index("wa_broadcast_log_campaign_status_idx", table_name="wa_broadcast_log")
    op.drop_index("wa_broadcast_log_wamid_idx", table_name="wa_broadcast_log")
    op.drop_table("wa_broadcast_log")
    op.drop_table("wa_suppression")
