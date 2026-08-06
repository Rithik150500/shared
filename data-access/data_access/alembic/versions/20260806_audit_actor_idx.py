"""audit_log.actor_id index — make "what did this person do" answerable

Revision ID: 20260806_audit_actor_idx
Revises: 20260717_supabase_user_id
Create Date: 2026-08-06

``audit_log`` has carried an ``actor_id`` column since it was created, and it
was NULL in all 13,313 rows: nothing ever passed it. On a shared book that made
the two interesting questions indistinguishable — ``user_id`` is the book OWNER
(resolved from ``client_id``), so a team member acting on someone else's book
left a row identical to the owner acting on their own.

casepilot now populates ``actor_id`` at the ``case.searched`` / ``case.added``
sites. This adds the index those reads need. Without it a
"show me everything this person did" query sequentially scans, because the
existing ``audit_log_user_id_idx`` is PARTIAL on ``user_id IS NOT NULL`` and
covers a different column anyway.

Partial on ``actor_id IS NOT NULL`` to mirror the user_id index: every
pre-existing row and every system-sourced event has a NULL actor, and indexing
those costs write throughput for no read benefit.

Deliberately NOT ``CONCURRENTLY``: the table is ~13k rows, so the build is
sub-second, and a concurrent build cannot run inside Alembic's transaction.
"""
from __future__ import annotations

from alembic import op

revision = "20260806_audit_actor_idx"
down_revision = "20260717_supabase_user_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "audit_log_actor_id_idx",
        "audit_log",
        ["actor_id", "created_at"],
        unique=False,
        postgresql_where="actor_id IS NOT NULL",
        sqlite_where="actor_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index("audit_log_actor_id_idx", table_name="audit_log")
