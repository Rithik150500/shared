"""cases.search_tsv must index case_detail_md — the column it silently dropped

Revision ID: 20260806_case_search_detail
Revises: 20260806_audit_actor_idx
Create Date: 2026-08-06

``cases.search_tsv`` was introduced (20260523) to replace the SQLite
``cases_fts`` virtual table for case search in the command palette. Its own
docstring states it covers "the same three fields the SQLite cases_fts FTS5
virtual table indexed".

That is wrong. ``cases_fts`` is declared over FOUR columns —
``title, cnr, case_number, case_detail`` — and its trigger feeds ``case_detail``
from ``coalesce(new.case_detail_md, '')``. The generated column shipped with
three, so every word that appears only in the case detail became unsearchable
the moment case search moved to Postgres.

Measured on prod 2026-08-06: searching "GEDELA" (a party name present in the
detail of 7 cases) returned 0 hits.

This re-creates the generated column with ``case_detail_md`` included, matching
what SQLite actually indexed. A STORED generated column cannot be altered in
place, so the column and its GIN index are dropped and rebuilt; on ~1,300 rows
that is effectively instant.
"""
from __future__ import annotations

from alembic import op

revision = "20260806_case_search_detail"
down_revision = "20260806_audit_actor_idx"
branch_labels = None
depends_on = None


_WITH_DETAIL = """
    ALTER TABLE cases
    ADD COLUMN search_tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector(
            'english',
            coalesce(title, '') || ' ' ||
            coalesce(case_number, '') || ' ' ||
            coalesce(cnr, '') || ' ' ||
            coalesce(case_detail_md, '')
        )
    ) STORED
"""

_WITHOUT_DETAIL = """
    ALTER TABLE cases
    ADD COLUMN search_tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector(
            'english',
            coalesce(title, '') || ' ' ||
            coalesce(case_number, '') || ' ' ||
            coalesce(cnr, '')
        )
    ) STORED
"""


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_cases_search_tsv")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS search_tsv")
    op.execute(_WITH_DETAIL)
    op.execute("CREATE INDEX idx_cases_search_tsv ON cases USING gin(search_tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_cases_search_tsv")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS search_tsv")
    op.execute(_WITHOUT_DETAIL)
    op.execute("CREATE INDEX idx_cases_search_tsv ON cases USING gin(search_tsv)")
