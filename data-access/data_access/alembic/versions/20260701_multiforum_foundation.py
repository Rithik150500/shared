"""multi-forum foundation: forum / forum_case_ref / source on cases

Revision ID: 20260701_multiforum_foundation
Revises: 20260629_refresh_rotation
Create Date: 2026-07-01

Adds the multi-forum columns to ``cases`` so Supreme Court / Consumer / DRT /
Arbitration cases — and all manually-entered cases — can live alongside the
existing eCourts (district/highcourt) cases:

forum          Superset discriminator: ecourts_district | ecourts_highcourt |
               supreme_court | consumer | drt | arbitration. Backfilled from
               ``portal`` for existing (all-eCourts) rows.
forum_case_ref Universal per-forum identity: == cnr for eCourts (backfilled),
               the user's normalized case number for other forums, or a
               synthetic 'm-<uuid>' for a manual case with no official number.
source         ecourts_auto | manual | ejagriti_auto | drt_auto | sc_auto.
               Drives refresh gating — get_due_for_refresh only returns *_auto
               rows, so manual rows are never polled.

``cnr`` and ``portal`` become nullable (eCourts-only). The old (user_id, cnr)
UNIQUE constraint becomes a PARTIAL unique index (WHERE cnr IS NOT NULL) so
multiple NULL-cnr rows never collide; a new (user_id, forum, forum_case_ref)
UNIQUE keys every forum. CHECKs are added for forum/source plus a forum<->portal
consistency guard, and the portal CHECK is relaxed to allow NULL.

This is ADDITIVE to ``cases`` only (no user_id / FK re-key) → no interaction
with the Sub-project-G id cutover; it slots in as the next revision after
20260629_refresh_rotation. All constraint DDL is Postgres-only (the SQLite unit
-test path builds the schema from the model via create_all, not this migration).
Revision id is 30 chars (<= VARCHAR(32)).

downgrade() refuses to run if any non-eCourts rows exist (cnr IS NULL), since
restoring ``cnr NOT NULL`` would silently lose them.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260701_multiforum_foundation"
down_revision = "20260629_refresh_rotation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # 1. Add columns nullable/defaulted so ADD COLUMN is instant on PG 11+.
    op.add_column(
        "cases",
        sa.Column(
            "forum", sa.Text(), nullable=False,
            server_default=sa.text("'ecourts_district'"),
        ),
    )
    op.add_column("cases", sa.Column("forum_case_ref", sa.Text(), nullable=True))
    op.add_column(
        "cases",
        sa.Column(
            "source", sa.Text(), nullable=False,
            server_default=sa.text("'ecourts_auto'"),
        ),
    )

    # 2. Backfill existing (all-eCourts) rows.
    op.execute(
        "UPDATE cases SET forum = CASE WHEN portal = 'highcourt' "
        "THEN 'ecourts_highcourt' ELSE 'ecourts_district' END"
    )
    op.execute("UPDATE cases SET source = 'ecourts_auto'")
    op.execute("UPDATE cases SET forum_case_ref = cnr WHERE forum_case_ref IS NULL")

    if not is_pg:
        # SQLite (test-only alembic runs) can't ALTER constraints; the model's
        # create_all already expresses the final shape there. Column adds +
        # backfill above are enough for a chained SQLite upgrade to succeed.
        return

    # 3. forum_case_ref NOT NULL now that it is backfilled.
    op.alter_column("cases", "forum_case_ref", nullable=False)

    # 4. cnr + portal become eCourts-only (nullable).
    op.alter_column("cases", "cnr", existing_type=sa.String(16), nullable=True)
    op.alter_column("cases", "portal", existing_type=sa.Text(), nullable=True)

    # 5. Relax the portal CHECK to allow NULL for non-eCourts rows.
    op.drop_constraint("cases_portal_check", "cases", type_="check")
    op.create_check_constraint(
        "cases_portal_check", "cases",
        "portal IS NULL OR portal IN ('district', 'highcourt')",
    )

    # 6. New forum / source / consistency CHECKs.
    op.create_check_constraint(
        "cases_forum_check", "cases",
        "forum IN ('ecourts_district', 'ecourts_highcourt', 'supreme_court', "
        "'consumer', 'drt', 'arbitration')",
    )
    op.create_check_constraint(
        "cases_source_check", "cases",
        "source IN ('ecourts_auto', 'manual', 'ejagriti_auto', 'drt_auto', "
        "'sc_auto')",
    )
    op.create_check_constraint(
        "cases_forum_portal_consistency", "cases",
        "(forum = 'ecourts_district'  AND portal = 'district')  OR "
        "(forum = 'ecourts_highcourt' AND portal = 'highcourt') OR "
        "(forum NOT IN ('ecourts_district', 'ecourts_highcourt'))",
    )

    # 7. Swap the eCourts uniqueness: table UNIQUE constraint -> partial unique
    #    index (name reused once the constraint + its backing index are gone).
    op.drop_constraint("cases_user_cnr_unique", "cases", type_="unique")
    op.create_index(
        "cases_user_cnr_unique", "cases", ["user_id", "cnr"],
        unique=True, postgresql_where=sa.text("cnr IS NOT NULL"),
    )

    # 8. New universal per-forum uniqueness.
    op.create_unique_constraint(
        "cases_user_forum_ref_unique", "cases",
        ["user_id", "forum", "forum_case_ref"],
    )

    # 9. Drop the temporary server_defaults; app-side defaults own values now.
    op.alter_column("cases", "forum", server_default=None)
    op.alter_column("cases", "source", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        # Refuse a lossy downgrade: non-eCourts rows have a NULL cnr and cannot
        # satisfy a restored cnr NOT NULL.
        n = bind.execute(
            sa.text("SELECT count(*) FROM cases WHERE cnr IS NULL")
        ).scalar()
        if n:
            raise RuntimeError(
                f"Cannot downgrade: {n} non-eCourts case row(s) have a NULL cnr; "
                "restoring cnr NOT NULL would lose them. Remove/migrate them first."
            )
        op.drop_constraint("cases_user_forum_ref_unique", "cases", type_="unique")
        op.drop_index("cases_user_cnr_unique", table_name="cases")
        op.create_unique_constraint(
            "cases_user_cnr_unique", "cases", ["user_id", "cnr"],
        )
        op.drop_constraint("cases_forum_portal_consistency", "cases", type_="check")
        op.drop_constraint("cases_source_check", "cases", type_="check")
        op.drop_constraint("cases_forum_check", "cases", type_="check")
        op.drop_constraint("cases_portal_check", "cases", type_="check")
        op.create_check_constraint(
            "cases_portal_check", "cases", "portal IN ('district', 'highcourt')",
        )
        op.alter_column("cases", "portal", existing_type=sa.Text(), nullable=False)
        op.alter_column("cases", "cnr", existing_type=sa.String(16), nullable=False)

    op.drop_column("cases", "source")
    op.drop_column("cases", "forum_case_ref")
    op.drop_column("cases", "forum")
