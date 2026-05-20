"""sub-project C Phase 1: munshi_upsell_events + users_nowlez merge banner cols

Revision ID: 20260605_c_upsell
Revises: 20260603_a_completion
Create Date: 2026-05-20

Adds the backend schema kernel for sub-project C (cross-tier UX):
- munshi_upsell_events: tracks per-user upsell stage progression (initial,
  reminder, final), trigger reason, conversion outcome.
- users_nowlez.merge_banner_dismissed + merge_banner_dismissed_at: amendment
  to sub-project D for the welcome banner UX.

down_revision points at A-completion's case_preferences migration.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260605_c_upsell"
down_revision = "20260603_a_completion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "munshi_upsell_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("trigger_reason", sa.Text, nullable=False),
        sa.Column("case_count_at_send", sa.Integer, nullable=False),
        sa.Column("spend_at_send_rupees", sa.BigInteger, nullable=False),
        sa.Column("template_name", sa.Text, nullable=False),
        sa.Column("meta_message_id", sa.Text, nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_to_tier", sa.Text, nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="munshi_upsell_events_pkey"),
        sa.UniqueConstraint(
            "user_id", "stage", name="munshi_upsell_events_unique_stage",
        ),
        sa.CheckConstraint(
            "stage IN ('initial', 'reminder', 'final')",
            name="munshi_upsell_events_stage_check",
        ),
        sa.CheckConstraint(
            "trigger_reason IN ('case_count', 'spend', 'both')",
            name="munshi_upsell_events_trigger_check",
        ),
    )
    op.create_index(
        "munshi_upsell_events_user_id_idx",
        "munshi_upsell_events",
        ["user_id", sa.text("sent_at DESC")],
    )
    op.create_index(
        "munshi_upsell_events_unconverted_idx",
        "munshi_upsell_events",
        ["sent_at"],
        postgresql_where=sa.text("converted_at IS NULL"),
    )

    # Amendment to sub-project D: merge banner state on users_nowlez.
    op.add_column(
        "users_nowlez",
        sa.Column(
            "merge_banner_dismissed",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    op.add_column(
        "users_nowlez",
        sa.Column(
            "merge_banner_dismissed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users_nowlez", "merge_banner_dismissed_at")
    op.drop_column("users_nowlez", "merge_banner_dismissed")
    op.drop_index(
        "munshi_upsell_events_unconverted_idx",
        table_name="munshi_upsell_events",
    )
    op.drop_index(
        "munshi_upsell_events_user_id_idx",
        table_name="munshi_upsell_events",
    )
    op.drop_table("munshi_upsell_events")
