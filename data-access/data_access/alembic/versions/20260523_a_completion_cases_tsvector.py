"""Sub-project A completion 5.3.g.1: add `cases.search_tsv` generated tsvector
column + GIN index for universal search.

After this migration the universal-search endpoint (backend/db/search.py
case branch, updated in 5.3.g.2) can query `cases.search_tsv @@ plainto_tsquery(...)`
to surface case hits in the command palette without round-tripping through
the legacy SQLite `cases_fts` virtual table (which is dropped in PR 6
along with the rest of the SQLite case tables).

The generated column covers `title || ' ' || case_number || ' ' || cnr` —
the same three fields the SQLite cases_fts FTS5 virtual table indexed.

Revision ID: 20260523_a_completion_cases_tsvector
Revises: 20260606_b3_dedup
Create Date: 2026-05-23
"""

from alembic import op


revision = "20260523_a_completion_cases_tsvector"
down_revision = "20260606_b3_dedup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `STORED` so the tsvector is materialised on insert/update rather than
    # recomputed per query. Postgres FTS over 100k+ rows is essentially
    # free at query time when paired with a GIN index on the stored column.
    op.execute("""
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
    """)
    op.execute(
        "CREATE INDEX idx_cases_search_tsv ON cases USING gin(search_tsv)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_cases_search_tsv")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS search_tsv")
