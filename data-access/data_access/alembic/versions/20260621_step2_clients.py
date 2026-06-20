"""step2: create teams + clients tables

Revision ID: 20260621_step2_clients
Revises: 20260620_g_orphan_cols
Create Date: 2026-06-21

Step-2 SQLite→PG cutover. Creates the `clients` table (16-hex TEXT natural key
kept verbatim from SQLite — PG cases.client_id already stores the 16-hex string
as a denormalized passthrough, so keeping the key avoids a shim rewrite) plus
the minimal `teams` table that is its `team_id` FK target.

`teams` is created FIRST in upgrade() (and dropped LAST in downgrade()) so the
`clients.team_id -> teams.id` foreign key always resolves. server_defaults are
set explicitly here (the ORM models omit gen_random_uuid()-style literals for
SQLite compatibility — prod schema is preserved via this migration, per the same
pattern used in 0002_add_case_tables.py).

down_revision targets the current head (`20260620_g_orphan_cols`).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260621_step2_clients"
down_revision = "20260620_g_orphan_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # teams first: it is the FK target for clients.team_id.
    op.create_table(
        "teams",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), nullable=False,
        ),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False, server_default="free"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("teams_owner_id_idx", "teams", ["owner_id"])

    op.create_table(
        "clients",
        # 16-hex TEXT natural key, verbatim from SQLite.
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column(
            "is_demo", sa.Boolean(), nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("clients_user_id_idx", "clients", ["user_id"])
    op.create_index(
        "clients_user_created_idx", "clients",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "clients_team_id_idx", "clients", ["team_id"],
        postgresql_where=sa.text("team_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("clients_team_id_idx", table_name="clients")
    op.drop_index("clients_user_created_idx", table_name="clients")
    op.drop_index("clients_user_id_idx", table_name="clients")
    op.drop_table("clients")
    op.drop_index("teams_owner_id_idx", table_name="teams")
    op.drop_table("teams")
