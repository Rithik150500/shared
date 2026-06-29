"""google sign-in: user_external_identities (federated identity link)

Revision ID: 20260629_google_identity
Revises: 20260625_e164_phone_canon
Create Date: 2026-06-29

Adds the ``user_external_identities`` table backing "Sign in with Google".

A dedicated table (not a ``users_nowlez.google_sub`` column) mirrors how the
unified-auth work separated otp_codes / email_otp_codes / login_requests, and
generalizes to additional OAuth providers later. The provider ``sub`` (Google's
stable subject id) is the authoritative login anchor — login resolves on
``(provider, provider_sub)`` first, then falls back to the verified email.

Two UNIQUE constraints: ``(provider, provider_sub)`` (one account per Google
identity) and ``(user_id, provider)`` (a user links at most one Google account),
plus a non-unique index on ``user_id`` for the reverse lookup.

down_revision chains onto 20260625_e164_phone_canon — the single head per
``alembic heads`` on 2026-06-29. Revision id is 24 chars (<= VARCHAR(32)).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260629_google_identity"
down_revision = "20260625_e164_phone_canon"
branch_labels = None
depends_on = None


# Native UUID on Postgres, String(36) on SQLite (unit-test path) — copied from
# 20260619_unified_login.py.
def _uuid_col(dialect_name: str) -> sa.types.TypeEngine:
    if dialect_name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(36)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    _uuid = _uuid_col(dialect)
    is_pg = dialect == "postgresql"
    id_server_default = sa.text("gen_random_uuid()") if is_pg else None

    op.create_table(
        "user_external_identities",
        sa.Column("id", _uuid, primary_key=True, server_default=id_server_default),
        sa.Column(
            "user_id",
            _uuid,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_sub", sa.Text(), nullable=False),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('google')",
            name="user_external_identities_provider_check",
        ),
        sa.UniqueConstraint(
            "provider", "provider_sub", name="user_external_identities_provider_sub_key"
        ),
        sa.UniqueConstraint(
            "user_id", "provider", name="user_external_identities_user_provider_key"
        ),
    )
    op.create_index(
        "user_external_identities_user_id_idx",
        "user_external_identities",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "user_external_identities_user_id_idx",
        table_name="user_external_identities",
    )
    op.drop_table("user_external_identities")
