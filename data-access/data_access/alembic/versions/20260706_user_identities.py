"""multi-identity aliases: user_identities (phone/email alias -> account)

Revision ID: 20260706_user_identities
Revises: 20260705_tribunal_family
Create Date: 2026-07-06

Adds ``user_identities`` — a phone/email identity that routes to a core users
row. Phase-1 home for alias identities (a second phone recognised by the bot, a
second email that can OTP-login) and the intended future superset that
user_external_identities (OAuth) folds into. Only verified_at IS NOT NULL rows
route/authenticate; UNIQUE(kind, value) keeps a value on at most one account.

down_revision chains onto 20260705_tribunal_family, the current single data_access
head per ``alembic heads`` (after the tribunal-family migration, which itself
chains onto 20260701_multiforum_foundation). Revision id is 24 chars (<= VARCHAR(32)). create_table
is dialect-safe, but the SQLite unit-test path builds this from the model via
create_all, not this migration.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260706_user_identities"
down_revision = "20260705_tribunal_family"
branch_labels = None
depends_on = None


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
        "user_identities",
        sa.Column("id", _uuid, primary_key=True, server_default=id_server_default),
        sa.Column(
            "user_id",
            _uuid,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("added_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('phone', 'email')", name="user_identities_kind_check"),
        sa.UniqueConstraint("kind", "value", name="user_identities_kind_value_key"),
    )
    op.create_index("user_identities_user_id_idx", "user_identities", ["user_id"])


def downgrade() -> None:
    op.drop_index("user_identities_user_id_idx", table_name="user_identities")
    op.drop_table("user_identities")
