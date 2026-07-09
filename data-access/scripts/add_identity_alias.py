"""Operator CLI: attach a verified/pending phone or email alias to an account.

Runs against the shared Postgres (DATABASE_URL). Reclaims an empty colliding
orphan or refuses (AliasConflictError) if the colliding account owns data.

Usage (inside a container that has data_access + DATABASE_URL):
    python scripts/add_identity_alias.py \
        --user-id eecaa1f7-5724-4a82-9713-9a87ed4d4518 \
        --kind phone --value 8882271502 --verified --added-by op:rithik
"""
from __future__ import annotations

import argparse
import os
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from data_access.daos import audit_dao, identity_alias_dao


def run(session, *, user_id, kind, value, verified, added_by, reclaim):
    if isinstance(user_id, str):
        user_id = uuid.UUID(user_id)
    row = identity_alias_dao.add_alias(
        session, user_id=user_id, kind=kind, value=value,
        verified=verified, added_by=added_by, reclaim_orphan=reclaim,
    )
    if row is not None:
        # Spec §C: alias writes are audited. Operator layer owns the audit in P1
        # (source='nowlez' is a known-valid AuditLog.source; P2 self-service will
        # audit at its own layer). No-op returns (own-primary) are not audited.
        audit_dao.log_event(
            session, event_type="identity.alias_added", source="nowlez",
            user_id=user_id,
            metadata={
                "kind": row.kind, "value": row.value,
                "verified": row.verified_at is not None, "added_by": added_by,
            },
        )
    return row


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Add a phone/email alias to an account.")
    p.add_argument("--user-id", required=True)
    p.add_argument("--kind", required=True, choices=["phone", "email"])
    p.add_argument("--value", required=True)
    p.add_argument("--verified", action="store_true", help="force-verify (operator vouches)")
    p.add_argument("--added-by", default="operator")
    p.add_argument("--no-reclaim", dest="reclaim", action="store_false")
    p.add_argument("--dry-run", action="store_true", help="roll back instead of commit")
    args = p.parse_args(argv)

    url = os.environ["DATABASE_URL"]
    engine = create_engine(url, future=True)
    factory: sessionmaker[Session] = sessionmaker(bind=engine, future=True)
    with factory() as session:
        row = run(
            session, user_id=args.user_id, kind=args.kind, value=args.value,
            verified=args.verified, added_by=args.added_by, reclaim=args.reclaim,
        )
        if args.dry_run:
            session.rollback()
            print(f"[dry-run] would add: {row and (row.kind, row.value, row.verified_at)}")
        else:
            session.commit()
            print(f"OK: {row and (row.kind, row.value, 'verified' if row.verified_at else 'pending')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
