"""tribunal family: generic `tribunal` forum + tribunal_kind sub-type on cases

Revision ID: 20260705_tribunal_family
Revises: 20260701_multiforum_foundation
Create Date: 2026-07-05

Adds the generic tribunals family as ONE ``forum='tribunal'`` value sub-typed by
a new ``tribunal_kind`` column (nclt / nclat / cat / itat / ngt / tdsat / aft /
cestat / drt / drat / sat …), rather than a distinct forum per tribunal. New
tribunals are pure data (a TribunalKind enum member) — no further migration.

tribunal_kind   Set IFF forum='tribunal'; NULL for every other forum. The
                structural analog of ``portal`` for the eCourts family. Read hot
                by capability + refresh routing, and part of the tribunal
                uniqueness key.
source          += 'tribunal_auto' (one generic auto source; capability is
                derived per (forum, kind) at the adapter registry, not per source).

Uniqueness is SPLIT so a shared 'tribunal' forum (many kinds) can't weaken the
eCourts/consumer collision key:
  cases_user_forum_ref_unique     (user_id, forum, forum_case_ref)            WHERE tribunal_kind IS NULL
  cases_user_tribunal_ref_unique  (user_id, forum, tribunal_kind, forum_case_ref) WHERE tribunal_kind IS NOT NULL

DRT FOLD: the currently-manual standalone ``forum='drt'`` rows are folded into
``forum='tribunal', tribunal_kind='drt'`` (a bijection on the old (user,drt,ref)
unique key → no new collision). ``Forum.DRT`` / ``source='drt_auto'`` / the
'drt' forum-CHECK value are kept ONE release (mid-deploy forward-compat for an
old worker still writing forum='drt'), retired in a later cleanup revision. The
forum CHECK is widened to include 'tribunal' BEFORE the fold UPDATE so
``SET forum='tribunal'`` is legal.

ADDITIVE to ``cases`` only (no user_id / FK re-key) → no Sub-project-G collision;
next revision after 20260701_multiforum_foundation. All constraint/index DDL is
Postgres-only (SQLite test path builds the schema from the model via create_all).
Revision id is 24 chars (<= VARCHAR(32)).

downgrade() un-folds DRT (tribunal/kind=drt -> forum='drt'), then REFUSES if any
other ``forum='tribunal'`` rows remain (dropping the column would lose them).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260705_tribunal_family"
down_revision = "20260701_multiforum_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # 1. Add the sub-classifier column (nullable; set IFF forum='tribunal').
    op.add_column("cases", sa.Column("tribunal_kind", sa.Text(), nullable=True))

    if not is_pg:
        # SQLite (test-only alembic runs) can't ALTER constraints/indexes; the
        # model's create_all already expresses the final shape there. The column
        # add is enough for a chained SQLite upgrade to succeed. (No live 'drt'
        # rows exist in a fresh test DB, so the fold below is a PG-only concern.)
        return

    # 2. Widen the forum CHECK to allow 'tribunal' BEFORE the fold UPDATE (keep
    #    'drt' one release for forward-compat).
    op.drop_constraint("cases_forum_check", "cases", type_="check")
    op.create_check_constraint(
        "cases_forum_check", "cases",
        "forum IN ('ecourts_district', 'ecourts_highcourt', 'supreme_court', "
        "'consumer', 'drt', 'arbitration', 'tribunal')",
    )

    # 3. Extend the source CHECK with the generic tribunal auto source.
    op.drop_constraint("cases_source_check", "cases", type_="check")
    op.create_check_constraint(
        "cases_source_check", "cases",
        "source IN ('ecourts_auto', 'manual', 'ejagriti_auto', 'drt_auto', "
        "'sc_auto', 'tribunal_auto')",
    )

    # 4. DRT fold: standalone forum='drt' -> forum='tribunal', kind='drt'.
    #    A bijection on the old (user_id, 'drt', forum_case_ref) unique key, so it
    #    introduces no duplicate in the new tribunal unique index (no pre-existing
    #    forum='tribunal' rows can exist — the CHECK forbade the value until step 2).
    op.execute(
        "UPDATE cases SET forum = 'tribunal', tribunal_kind = 'drt' "
        "WHERE forum = 'drt'"
    )

    # 5. Consistency CHECK: tribunal_kind is set IFF the forum is 'tribunal'
    #    (created AFTER the fold so every row already satisfies it).
    op.create_check_constraint(
        "cases_tribunal_kind_consistency", "cases",
        "(forum = 'tribunal' AND tribunal_kind IS NOT NULL) OR "
        "(forum <> 'tribunal' AND tribunal_kind IS NULL)",
    )

    # 6. Split the per-forum uniqueness: the old table UNIQUE constraint becomes
    #    a partial unique index (non-tribunal rows), plus a new kind-scoped
    #    partial unique index (tribunal rows).
    op.drop_constraint("cases_user_forum_ref_unique", "cases", type_="unique")
    op.create_index(
        "cases_user_forum_ref_unique", "cases",
        ["user_id", "forum", "forum_case_ref"],
        unique=True, postgresql_where=sa.text("tribunal_kind IS NULL"),
    )
    op.create_index(
        "cases_user_tribunal_ref_unique", "cases",
        ["user_id", "forum", "tribunal_kind", "forum_case_ref"],
        unique=True, postgresql_where=sa.text("tribunal_kind IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        # Un-fold DRT first so those rows survive the downgrade.
        op.execute(
            "UPDATE cases SET forum = 'drt', tribunal_kind = NULL "
            "WHERE forum = 'tribunal' AND tribunal_kind = 'drt'"
        )
        # Refuse if any other tribunal rows remain — dropping the column + the
        # 'tribunal' forum value would lose them.
        n = bind.execute(
            sa.text("SELECT count(*) FROM cases WHERE forum = 'tribunal'")
        ).scalar()
        if n:
            raise RuntimeError(
                f"Cannot downgrade: {n} tribunal case row(s) remain (non-DRT kinds); "
                "removing the tribunal forum would lose them. Migrate/remove them first."
            )

        op.drop_index("cases_user_tribunal_ref_unique", table_name="cases")
        op.drop_index("cases_user_forum_ref_unique", table_name="cases")
        op.create_unique_constraint(
            "cases_user_forum_ref_unique", "cases",
            ["user_id", "forum", "forum_case_ref"],
        )
        op.drop_constraint("cases_tribunal_kind_consistency", "cases", type_="check")
        op.drop_constraint("cases_source_check", "cases", type_="check")
        op.create_check_constraint(
            "cases_source_check", "cases",
            "source IN ('ecourts_auto', 'manual', 'ejagriti_auto', 'drt_auto', 'sc_auto')",
        )
        op.drop_constraint("cases_forum_check", "cases", type_="check")
        op.create_check_constraint(
            "cases_forum_check", "cases",
            "forum IN ('ecourts_district', 'ecourts_highcourt', 'supreme_court', "
            "'consumer', 'drt', 'arbitration')",
        )

    op.drop_column("cases", "tribunal_kind")
