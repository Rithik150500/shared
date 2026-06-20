"""step2: create teams + team_members + clients tables

Revision ID: 20260621_step2_clients
Revises: 20260620_g_orphan_cols
Create Date: 2026-06-21

Step-2 SQLite→PG cutover. Creates the `clients` table (16-hex TEXT natural key
kept verbatim from SQLite — PG cases.client_id already stores the 16-hex string
as a denormalized passthrough, so keeping the key avoids a shim rewrite) plus
the minimal `teams` table that is its `team_id` FK target, and the
`team_members` membership table (UUID PK, UNIQUE(team_id, user_id)).

`teams` is created FIRST in upgrade() (and dropped LAST in downgrade()) so both
the `clients.team_id -> teams.id` and `team_members.team_id -> teams.id` foreign
keys always resolve. server_defaults are
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

    # team_members: depends on teams (team_id FK) + users (user_id/invited_by FKs).
    op.create_table(
        "team_members",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), nullable=False,
        ),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="viewer"),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("invite_token", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "team_id", "user_id", name="team_members_team_user_unique",
        ),
    )
    op.create_index("team_members_user_idx", "team_members", ["user_id"])
    op.create_index("team_members_team_idx", "team_members", ["team_id"])

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
    op.drop_index("team_members_team_idx", table_name="team_members")
    op.drop_index("team_members_user_idx", table_name="team_members")
    op.drop_table("team_members")
    op.drop_index("teams_owner_id_idx", table_name="teams")
    op.drop_table("teams")
