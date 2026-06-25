"""data: canonicalize users.phone to E.164 (one-time backfill)

Revision ID: 20260625_e164_phone_canon
Revises: 20260622_step4_content
Create Date: 2026-06-25

Closes the phone-format identity split (bare 10-digit web/admin rows vs the
``+91`` webhook rows that ``UNIQUE(phone)`` treats as different keys -> two user
rows / "portfolio is empty"). ``user_dao`` already normalizes at write time, so
no NEW drift appears; this one-time data migration cleans up rows written before
that fix by calling the already-tested ``reconcile_phone_formats(dry_run=False)``.

That routine renames each LONE non-canonical row to its E.164 form and
deliberately REFUSES to auto-merge a true collision (two rows for the same
number) — merging users means cascading FK decisions across cases/billing/
extensions and must be done by a human. Collisions are logged here (not touched).

Idempotent: a re-run finds nothing to rename. Downgrade is a no-op — the original
non-canonical formats are not recorded, so the rename is forward-only.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy.orm import Session


revision = "20260625_e164_phone_canon"
down_revision = "20260622_step4_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Import lazily so the module loads even if data_access isn't importable at
    # script-collection time; reconcile uses the same normalize_phone the
    # write-side path uses, so canonicalization is consistent by construction.
    from data_access.phone_reconcile import reconcile_phone_formats

    # Bind an ORM Session to alembic's connection so the writes share the
    # migration transaction (alembic commits it). Do NOT session.commit() here.
    session = Session(bind=op.get_bind())
    report = reconcile_phone_formats(session, dry_run=False)
    session.flush()
    print(
        "e164 canonicalize: renamed=%d collisions=%d (collisions left for human review)"
        % (len(report["renamed"]), len(report["collisions"]))
    )


def downgrade() -> None:
    # No-op: pre-canonicalization phone formats are not recorded, so the rename
    # cannot be reversed. The change is forward-only and idempotent.
    pass
