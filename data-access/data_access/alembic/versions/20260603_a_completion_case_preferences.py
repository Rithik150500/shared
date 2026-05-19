"""sub-project A completion: case_preferences table

Revision ID: 20260603_a_completion
Revises: 20260515_b_whatsapp
Create Date: 2026-05-20

Adds the case_preferences table that carries per-user-per-case Munshi
notification preferences (alert_level / snooze_until / digest_enabled).

Layered on top of `cases` via natural (user_id, cnr) key. Closes the
read-side gap from sub-project A Step 4: save_command writes to
data_access.Case but legacy bot_scaffold.SavedCase still carries the prefs
columns. This migration moves the prefs to the shared schema; the
0705-side cleanup (drop saved_cases table) happens in sub-project A
completion S6.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260603_a_completion"
down_revision = "20260515_b_whatsapp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_preferences",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cnr", sa.String(16), nullable=False),
        sa.Column("alert_level", sa.Text, nullable=False, server_default="all"),
        sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "digest_enabled", sa.Boolean,
            nullable=False, server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("user_id", "cnr", name="case_preferences_pkey"),
        sa.CheckConstraint(
            "alert_level IN ('all', 'orders_only', 'hearings_only', 'digest_only')",
            name="case_preferences_alert_level_check",
        ),
    )
    op.create_index(
        "case_preferences_user_id_idx", "case_preferences", ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("case_preferences_user_id_idx", table_name="case_preferences")
    op.drop_table("case_preferences")
