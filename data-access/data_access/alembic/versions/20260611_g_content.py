"""g: add uploaded_files + chat_history tables (content cohort)

Revision ID: 20260611_g_content
Revises: 20260610_g_teams
Create Date: 2026-05-28

Creates the Postgres content tables. uploaded_files is client-scoped.
chat_history carries a NOT-NULL user_id plus a nullable client_id: per-client
chats set client_id, unified (cross-client) chats leave it NULL (the legacy
SQLite ``__unified__{user_id}`` sentinel becomes user_id + NULL client_id).
Both carry a forensic legacy_sqlite_id used as the migration idempotency key.
sources_json / function_calls_json are kept as TEXT for a faithful copy
(JSONB is a possible later refinement).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260611_g_content"
down_revision = "20260610_g_teams"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "uploaded_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("descriptive_name", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("cnr", sa.Text(), nullable=True),
        sa.Column("document_type", sa.Text(), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("preprocessed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("permanently_failed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("legacy_sqlite_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uploaded_files_client_idx", "uploaded_files", ["client_id"])
    op.create_index(
        "ix_uploaded_files_legacy_sqlite_id", "uploaded_files", ["legacy_sqlite_id"],
        unique=True, postgresql_where=sa.text("legacy_sqlite_id IS NOT NULL"),
    )

    op.create_table(
        "chat_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sources_json", sa.Text(), nullable=True),
        sa.Column("function_calls_json", sa.Text(), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("legacy_sqlite_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("chat_history_user_idx", "chat_history", ["user_id", "created_at"])
    op.create_index("chat_history_client_idx", "chat_history", ["client_id"])
    op.create_index(
        "ix_chat_history_legacy_sqlite_id", "chat_history", ["legacy_sqlite_id"],
        unique=True, postgresql_where=sa.text("legacy_sqlite_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_chat_history_legacy_sqlite_id", "chat_history")
    op.drop_index("chat_history_client_idx", "chat_history")
    op.drop_index("chat_history_user_idx", "chat_history")
    op.drop_table("chat_history")
    op.drop_index("ix_uploaded_files_legacy_sqlite_id", "uploaded_files")
    op.drop_index("uploaded_files_client_idx", "uploaded_files")
    op.drop_table("uploaded_files")
