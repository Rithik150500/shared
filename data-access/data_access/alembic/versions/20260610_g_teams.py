"""g: add teams, team_members, pending_team_invites (client_mgmt cohort)

Revision ID: 20260610_g_teams
Revises: 20260609_g_clients
Create Date: 2026-05-28

Creates the Postgres team tables mirroring the legacy SQLite schema. teams gets
a forensic legacy_sqlite_id so the backfill can resolve team_members.team_id and
link clients.team_id. team_members uses a UUID PK + UNIQUE(team_id, user_id)
(the SQLite autoincrement id is not carried over). The clients.team_id FK is
intentionally NOT added here (kept a bare nullable UUID); it can be added in a
later tidy-up once the cohort has baked.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260610_g_teams"
down_revision = "20260609_g_clients"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tier", sa.Text(), server_default=sa.text("'free'"), nullable=False),
        sa.Column("legacy_sqlite_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("teams_owner_id_idx", "teams", ["owner_id"])
    op.create_index(
        "ix_teams_legacy_sqlite_id", "teams", ["legacy_sqlite_id"],
        unique=True, postgresql_where=sa.text("legacy_sqlite_id IS NOT NULL"),
    )

    op.create_table(
        "team_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), server_default=sa.text("'viewer'"), nullable=False),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("invite_token", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "user_id", name="team_members_team_user_uq"),
        sa.UniqueConstraint("invite_token", name="team_members_invite_token_uq"),
    )
    op.create_index("team_members_user_idx", "team_members", ["user_id"])

    op.create_table(
        "pending_team_invites",
        sa.Column("invite_token", sa.Text(), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("role", sa.Text(), server_default=sa.text("'viewer'"), nullable=False),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("invite_token"),
    )
    op.create_index("pending_team_invites_email_idx", "pending_team_invites", ["email"])


def downgrade() -> None:
    op.drop_index("pending_team_invites_email_idx", "pending_team_invites")
    op.drop_table("pending_team_invites")
    op.drop_index("team_members_user_idx", "team_members")
    op.drop_table("team_members")
    op.drop_index("ix_teams_legacy_sqlite_id", "teams")
    op.drop_index("teams_owner_id_idx", "teams")
    op.drop_table("teams")
