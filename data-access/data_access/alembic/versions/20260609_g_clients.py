"""g: add clients table (Sub-project G client_mgmt cohort)

Revision ID: 20260609_g_clients
Revises: 20260608_g_email_unsubscribed
Create Date: 2026-05-28

Creates the Postgres ``clients`` table mirroring the legacy SQLite schema
(post-migration-23): ``user_id`` FK -> users (ON DELETE CASCADE), a nullable
``team_id`` (the teams FK lands with the teams cohort), and a forensic
``legacy_sqlite_id`` (unique partial index) so the user/clients backfill and
later cohorts can resolve old SQLite client ids.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260609_g_clients"
down_revision = "20260608_g_email_unsubscribed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clients",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("legacy_sqlite_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("clients_user_id_idx", "clients", ["user_id"])
    op.create_index(
        "ix_clients_legacy_sqlite_id",
        "clients",
        ["legacy_sqlite_id"],
        unique=True,
        postgresql_where=sa.text("legacy_sqlite_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_clients_legacy_sqlite_id", "clients")
    op.drop_index("clients_user_id_idx", "clients")
    op.drop_table("clients")
