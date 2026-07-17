"""supabase: add users_nowlez.supabase_user_id mapping column

Revision ID: 20260717_supabase_user_id
Revises: 20260706_user_identities
Create Date: 2026-07-17

Supabase Auth migration, Phase 2. Adds the additive mapping column that
links a Supabase ``auth.users.id`` (the JWT ``sub``) to the existing shared
Postgres ``users.id`` spine. Supabase is an identity provider placed *in
front of* the ``users`` table, not a replacement: the shared UUID stays the
ownership spine that Munshi and every FK depend on, and the Supabase id is
resolved once at the auth boundary.

This deliberately mirrors ``20260607_g_legacy_sqlite_id`` (nullable, unique
partial index, NO FK) — the same external-id-to-internal-UUID shape, since
Supabase is simply a third external id resolved at the same seam.

Nullable + partial-unique means this is a no-op until code reads it: every
existing row is NULL, nothing is rewritten, and the whole change is
reversible by ``downgrade`` alone.

down_revision targets ``20260706_user_identities``, the single linear head
per ``alembic heads`` on 2026-07-17. Note the revision id (25 chars) is kept
under the 32-char ``alembic_version`` column width, and that filename order
in versions/ does NOT track chain order here — only the revision graph does.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260717_supabase_user_id"
down_revision = "20260706_user_identities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users_nowlez",
        sa.Column("supabase_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Partial (NOT NULL only): every pre-migration row is NULL, and a plain
    # unique index would collapse them all into one conflicting key.
    op.create_index(
        "ix_users_nowlez_supabase_user_id",
        "users_nowlez",
        ["supabase_user_id"],
        unique=True,
        postgresql_where=sa.text("supabase_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_users_nowlez_supabase_user_id", table_name="users_nowlez")
    op.drop_column("users_nowlez", "supabase_user_id")
