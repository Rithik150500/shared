"""Detect and reconcile phone-format drift in the ``users`` table.

Background: the WhatsApp webhook stores phones as E.164 (``+91...``) while the
web/OTP provisioning path historically stored bare 10-digit strings. Because
``UNIQUE(phone)`` treats them as different keys, a single person could end up as
two user rows — the "portfolio is empty" identity split. ``user_dao`` now
normalizes at write time so new drift can't appear; this module cleans up rows
written before the fix and monitors for any residual/future drift.

Two entry points:

* ``find_phone_format_collisions(session)`` — the guard. Returns
  ``{canonical_phone: [User, ...]}`` for every group of rows that normalize to
  the same number but are stored differently. Wire it into a health cron /
  alert so a regression surfaces loudly instead of as a silent empty portfolio.

* ``reconcile_phone_formats(session, dry_run=True)`` — the one-time fix. Safely
  renames each *lone* non-canonical row (no canonical twin) to its E.164 form.
  It deliberately REFUSES to auto-merge a true collision (two rows for the same
  number): merging users means cascading FK decisions over cases / billing /
  extensions, which must be done by a human. Collisions are reported, not
  touched. Run as a module: ``python -m data_access.phone_reconcile --commit``.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User
from .phone import normalize_phone


def _users_with_phone(session: Session) -> list[User]:
    return list(
        session.execute(select(User).where(User.phone.is_not(None))).scalars()
    )


def find_phone_format_collisions(session: Session) -> dict[str, list[User]]:
    """Return canonical_phone -> [User, ...] for rows that collide on normalize."""
    by_canon: dict[str, list[User]] = {}
    for u in _users_with_phone(session):
        canon = normalize_phone(u.phone)
        if canon is None:
            continue
        by_canon.setdefault(canon, []).append(u)
    return {canon: rows for canon, rows in by_canon.items() if len(rows) > 1}


def reconcile_phone_formats(session: Session, *, dry_run: bool = True) -> dict:
    """Canonicalize lone non-canonical rows; report (never auto-merge) collisions.

    Returns ``{"renamed": [{id, old, new}], "collisions": [{canonical, ids}]}``.
    With ``dry_run=True`` (default) the plan is computed but nothing is written.

    Run ``--commit`` during a quiet window: it scans, then UPDATEs, so a
    concurrent live INSERT of a colliding format between the scan and the commit
    could in theory make a rename hit ``UNIQUE(phone)``. The read-only
    ``find_phone_format_collisions`` health check is safe to run anytime.
    """
    collisions = find_phone_format_collisions(session)
    collision_ids = {u.id for rows in collisions.values() for u in rows}

    renamed: list[dict] = []
    for u in _users_with_phone(session):
        if u.id in collision_ids:
            continue  # part of a collision — leave for human review
        canon = normalize_phone(u.phone)
        if canon is not None and canon != u.phone:
            renamed.append({"id": str(u.id), "old": u.phone, "new": canon})
            if not dry_run:
                u.phone = canon

    if not dry_run:
        session.flush()

    return {
        "renamed": renamed,
        "collisions": [
            {"canonical": canon, "ids": [str(u.id) for u in rows]}
            for canon, rows in collisions.items()
        ],
    }


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    from .engine import get_session

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="apply changes (default is a dry run that only prints the plan)",
    )
    args = parser.parse_args(argv)

    with get_session() as session:
        report = reconcile_phone_formats(session, dry_run=not args.commit)
        if args.commit:
            session.commit()
    report["dry_run"] = not args.commit
    print(json.dumps(report, indent=2))
    # Non-zero exit when collisions need a human, so CI/cron can alert.
    return 1 if report["collisions"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
