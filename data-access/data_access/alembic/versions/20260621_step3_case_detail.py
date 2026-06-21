"""Step-3 cases cohort: add detail-blob columns to cases (D1).

Revision ID: 20260621_step3_case_detail
Revises: 20260621_step2_clients
Create Date: 2026-06-21

Adds case_detail_json (JSONB), case_detail_md (TEXT), mini_case_detail_md
(TEXT) so the disk+SQLite detail blobs migrate INTO Postgres (durability for
the eventual SQLite drop). Does NOT touch search_tsv — that generated column +
idx_cases_search_tsv already exist (20260523_a_completion_cases_tsvector). The
optional search_tsv rebuild to fold in case_detail_md is a DEPLOY-ops step
(runbook S5b), not part of this revision. Revision id is 26 chars (<= 32).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260621_step3_case_detail"
down_revision = "20260621_step2_clients"
branch_labels = None
depends_on = None


def _json_col(dialect_name: str) -> sa.types.TypeEngine:
    if dialect_name == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.add_column("cases", sa.Column("case_detail_json", _json_col(dialect), nullable=True))
    op.add_column("cases", sa.Column("case_detail_md", sa.Text(), nullable=True))
    op.add_column("cases", sa.Column("mini_case_detail_md", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("cases", "mini_case_detail_md")
    op.drop_column("cases", "case_detail_md")
    op.drop_column("cases", "case_detail_json")
