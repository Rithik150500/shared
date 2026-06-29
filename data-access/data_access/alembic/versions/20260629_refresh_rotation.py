"""refresh-token rotation: auth_sessions.family_id + replaced_by

Revision ID: 20260629_refresh_rotation
Revises: 20260629_google_identity
Create Date: 2026-06-29

Adds two columns to ``auth_sessions`` so the /refresh endpoint can ROTATE the
refresh token on every use (issue a new opaque token, revoke the presented one)
with reuse-detection:

family_id   Groups every rotation of a single login into one lineage. A detected
            replay of a rotated token revokes the whole family. Backfilled to the
            row's own id for pre-existing sessions (each is its own family).
replaced_by Successor session id, set when a row is rotated away. NULL on live
            rows and on explicitly-revoked (logout / password-change) rows — the
            NULL is how reuse-detection tells a rotated token (replay = theft)
            from a logged-out one (replay = plain invalid). Not a FK by design
            (internal pointer; avoids a self-referential CASCADE).

Plus auth_sessions_family_id_idx for the family-revoke lookup.

family_id is added nullable, backfilled (= id), then set NOT NULL on Postgres
(SQLite can't ALTER ... SET NOT NULL, and Base.metadata.create_all already makes
it NOT NULL there for the unit-test path). down_revision chains onto
20260629_google_identity, the single head on 2026-06-29. Revision id is 25 chars
(<= VARCHAR(32)).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260629_refresh_rotation"
down_revision = "20260629_google_identity"
branch_labels = None
depends_on = None


def _uuid_col(dialect_name: str) -> sa.types.TypeEngine:
    if dialect_name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(36)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    is_pg = dialect == "postgresql"
    _uuid = _uuid_col(dialect)

    op.add_column("auth_sessions", sa.Column("family_id", _uuid, nullable=True))
    op.add_column("auth_sessions", sa.Column("replaced_by", _uuid, nullable=True))
    # Backfill: each pre-rotation session becomes its own family.
    op.execute("UPDATE auth_sessions SET family_id = id WHERE family_id IS NULL")
    if is_pg:
        op.alter_column("auth_sessions", "family_id", nullable=False)
    op.create_index("auth_sessions_family_id_idx", "auth_sessions", ["family_id"])


def downgrade() -> None:
    op.drop_index("auth_sessions_family_id_idx", table_name="auth_sessions")
    op.drop_column("auth_sessions", "replaced_by")
    op.drop_column("auth_sessions", "family_id")
