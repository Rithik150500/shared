"""Step-4 content/notifications cohort: 3 new PG tables + dormant tsvector.

Revision ID: 20260622_step4_content
Revises: 20260621_step3_case_detail
Create Date: 2026-06-22

Creates uploaded_files_nowlez / chat_history_nowlez / notifications_nowlez.
search_tsv GENERATED columns + GIN indexes are Postgres-only (emitted only on
the postgresql dialect; the SQLite test variant gets the tables without them).
Revision id is 22 chars (<= 32) — the spec's 36-char id would break
alembic_version.version_num VARCHAR(32) (memory: Deploy gate fix 2026-06-09).
Applied BY HAND at deploy (prod alembic is pinned, runbook S1).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260622_step4_content"
down_revision = "20260621_step3_case_detail"
branch_labels = None
depends_on = None


def _json_col(dialect: str):
    return postgresql.JSONB() if dialect == "postgresql" else sa.JSON()


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    is_pg = dialect == "postgresql"

    op.create_table(
        "uploaded_files_nowlez",
        sa.Column("id", postgresql.UUID(as_uuid=True) if is_pg else sa.String(36),
                  primary_key=True, server_default=sa.text("gen_random_uuid()") if is_pg else None),
        sa.Column("legacy_sqlite_id", sa.BigInteger(), nullable=True),
        sa.Column("client_id", sa.Text(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("descriptive_name", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("cnr", sa.Text(), nullable=True),
        sa.Column("document_type", sa.Text(), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("file_storage", sa.Text(), nullable=False, server_default="local"),
        sa.Column("r2_object_key", sa.Text(), nullable=True),
        sa.Column("r2_etag", sa.Text(), nullable=True),
        sa.Column("preprocessed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("permanently_failed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("legacy_sqlite_id", name="uploaded_files_nowlez_legacy_id_uniq"),
        sa.CheckConstraint("file_storage IN ('local','r2')", name="uploaded_files_nowlez_storage_check"),
        sa.CheckConstraint("file_storage <> 'r2' OR r2_object_key IS NOT NULL",
                           name="uploaded_files_nowlez_r2_key_present"),
    )
    op.create_index("idx_uploaded_files_nowlez_client_id", "uploaded_files_nowlez", ["client_id"])
    op.create_index("idx_uploaded_files_nowlez_cnr", "uploaded_files_nowlez", ["cnr"])

    op.create_table(
        "chat_history_nowlez",
        sa.Column("id", postgresql.UUID(as_uuid=True) if is_pg else sa.String(36),
                  primary_key=True, server_default=sa.text("gen_random_uuid()") if is_pg else None),
        sa.Column("legacy_sqlite_id", sa.BigInteger(), nullable=True),
        sa.Column("client_id", sa.Text(), nullable=False),  # NON-ENFORCED FK (unified synthetic key)
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sources_json", _json_col(dialect), nullable=True),
        sa.Column("function_calls_json", _json_col(dialect), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("legacy_sqlite_id", name="chat_history_nowlez_legacy_id_uniq"),
        sa.CheckConstraint("feedback IS NULL OR feedback IN ('up','down')",
                           name="chat_history_nowlez_feedback_check"),
    )
    op.create_index("idx_chat_history_nowlez_client_id", "chat_history_nowlez", ["client_id"])
    op.create_index("idx_chat_history_nowlez_created_at", "chat_history_nowlez", ["client_id", "created_at"])

    op.create_table(
        "notifications_nowlez",
        sa.Column("id", postgresql.UUID(as_uuid=True) if is_pg else sa.String(36),
                  primary_key=True, server_default=sa.text("gen_random_uuid()") if is_pg else None),
        sa.Column("legacy_sqlite_id", sa.BigInteger(), nullable=True),
        sa.Column("client_id", sa.Text(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True) if is_pg else sa.String(36),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("case_cnr", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("dedup_key", sa.Text(), nullable=True),
        sa.UniqueConstraint("legacy_sqlite_id", name="notifications_nowlez_legacy_id_uniq"),
        sa.UniqueConstraint("dedup_key", name="notifications_nowlez_dedup_key_uniq"),
    )
    op.create_index("idx_notifications_nowlez_user_id", "notifications_nowlez", ["user_id", "created_at"])
    op.create_index("idx_notifications_nowlez_case", "notifications_nowlez", ["client_id", "case_cnr"])

    # --- Postgres-only: dormant tsvector generated columns + partial/GIN indexes ---
    if is_pg:
        op.execute(
            "ALTER TABLE uploaded_files_nowlez ADD COLUMN search_tsv tsvector "
            "GENERATED ALWAYS AS ("
            "to_tsvector('english', coalesce(original_filename,'') || ' ' || "
            "coalesce(descriptive_name,'') || ' ' || coalesce(summary,''))) STORED"
        )
        op.execute(
            "ALTER TABLE chat_history_nowlez ADD COLUMN search_tsv tsvector "
            "GENERATED ALWAYS AS (to_tsvector('english', coalesce(content,''))) STORED"
        )
        op.execute("CREATE INDEX idx_uploaded_files_nowlez_search_tsv "
                   "ON uploaded_files_nowlez USING GIN (search_tsv)")
        op.execute("CREATE INDEX idx_chat_history_nowlez_search_tsv "
                   "ON chat_history_nowlez USING GIN (search_tsv)")
        op.execute("CREATE INDEX idx_uploaded_files_nowlez_failed "
                   "ON uploaded_files_nowlez(client_id) "
                   "WHERE preprocessed = false AND permanently_failed = false")
        op.execute("CREATE INDEX idx_notifications_nowlez_user_unread "
                   "ON notifications_nowlez(user_id) WHERE is_read = false")


def downgrade() -> None:
    op.drop_table("notifications_nowlez")
    op.drop_table("chat_history_nowlez")
    op.drop_table("uploaded_files_nowlez")
