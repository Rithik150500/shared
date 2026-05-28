"""Sub-project A R2 — back-fill legacy SQLite users into Postgres.

Run AFTER the ``users.email`` Alembic migration (plan Task 4) is applied, AND
with a ``data_access`` pin that includes ``users_nowlez.legacy_sqlite_id``
(Sub-G I5) — the script references that column, so a stale pin crashes on row 1.

PR 6 cut casepilot's case data to Postgres but never migrated the legacy
SQLite ``users`` into Postgres ``users`` / ``users_nowlez`` — so the new
``_resolve_pg_user_id`` had nothing to resolve to. This one-shot script
closes that gap using **email** as the bridge key (the deliberate
alternative to the never-built phone-bind campaign), recording the source
SQLite id in ``users_nowlez.legacy_sqlite_id``.

Source vs. denominator (live audit, 2026-05-28, n=1,731)
--------------------------------------------------------
The legacy SQLite ``users`` table is ~95% integration-test fixtures:

    ~1,630  *.invalid  (casepilot-test.invalid 1,618 + test.invalid 12)
        23  @test.com / @testx.com / @example.com
       ...  prod-test- / concurrent / nonadmin localparts
    -------
       ~73  pattern-clean candidates  (20 ever logged in, 53 dormant)
       ~70  after 3 manual drops (see _MANUAL_DROP_EMAILS)

So the "1,729 broken users" figure is a phantom — the real target is ~70.
``is_test_user`` / ``EXCLUDE_TEST_SQL`` below strip the fixtures. The
filter is a DENYLIST (exclude known test rows, keep everything else): wrongly
dropping a real lawyer is far worse than migrating a dormant account, so it
errs toward inclusion. ``assert_candidate_count`` fails loud if the surviving
set leaves an expected band — a stale denylist that let the ~1,658 junk rows
through is caught BEFORE it floods Postgres (same "surface loudly" discipline
as ``cutover_subproject_e.py``).

Field policy (what carries over per user)
-----------------------------------------
``users``:           email, password_hash (login preserved!), is_active,
                     last_login_at, created_at. phone=NULL (email-primary);
                     locale/timezone/updated_at via model defaults; a fresh
                     uuid4 id.
``users_nowlez``:    name, is_admin, tier (verbatim), legacy_sqlite_id,
                     created_at. All usage counters / *_sent flags / consent
                     flags / onboarding_state via model defaults.

NOT carried (deliberate TODOs — confirm before prod, do not silently fake):
  * referred_by / referral_code — ``referred_by`` is an FK to a Postgres
    ``users.id`` UUID that doesn't exist until everyone is migrated. Remap
    the referral graph in a SECOND pass after this backfill, not here.
  * tier trial reset — ``cutover_subproject_e`` rewrote ``tier='free' ->
    NULL + 30d trial`` for the 2 existing PG users. Decide whether
    back-filled legacy users get the same gesture or keep their SQLite tier
    verbatim (current behaviour). See ``_TODO_TIER_POLICY``.
  * razorpay_*/billing-residual — Sub-BR cohort territory; left at defaults.

Idempotency & self-healing
--------------------------
Safe to re-run. Per source user, in order:
  * skip if a ``users_nowlez`` row already carries this ``legacy_sqlite_id``
    (already migrated);
  * if the email already exists in Postgres, HEAL rather than skip — create a
    missing ``users_nowlez`` extension, or set ``legacy_sqlite_id`` on an
    existing extension that lacks one. The existing user's auth
    (email / phone / password) is never modified;
  * only a genuine clash — the email is already linked to a *different*
    ``legacy_sqlite_id`` — is reported as ``email_conflict`` and skipped with
    a loud warning for manual reconciliation. (Cannot arise from this script
    alone: email is UNIQUE in the source SQLite.)

Note: the whole run is one transaction (single commit at the end), so a crash
mid-run rolls everything back — this script never *creates* a half-migrated
user. The heal path exists to complete half-states from *other* sources
(e.g. a phone-primary user who shares an email, or manual ops).

Entry points
------------
    python -m migrations.backfill_legacy_users --sqlite-path PATH            # live
    python -m migrations.backfill_legacy_users --sqlite-path PATH --dry-run  # counts only
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_access.models import User, UserNowlez


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Test-row filter (denylist). Patterns observed in the 2026-05-28 snapshot;
# `.invalid` and `example.com` are RFC 2606 reserved test names.
# --------------------------------------------------------------------------
_TEST_EMAIL_PATTERNS = (
    r".*\.invalid$",          # casepilot-test.invalid + test.invalid
    r".*@example\.com$",      # RFC 2606
    r".*@test\.com$",
    r".*@testx\.com$",
    r"^test@.*",
    r"^test\+.*",
    r".*\+e2e-.*",
    r"^prod-test-.*",
    r"^concurrent.*",
    r"^nonadmin.*",
)
_TEST_EMAIL_RE = re.compile("|".join(_TEST_EMAIL_PATTERNS), re.IGNORECASE)

# Manual review decisions for the 9 heuristic-flagged rows (2026-05-28). These
# pass the pattern denylist but were individually adjudicated:
#   DROP (unambiguous dev/test accounts):
#       abc123@gmail.com, lol1@gmail.com, test1604@gmail.com
#   KEEP (reviewed, migrated — documented so the flag doesn't resurface):
#       231087@clc.du.ac.in, 23309806833@clc.du.ac.in, 231897@clc.du.ac.in
#           (DU Campus Law Centre roll-number emails — real students)
#       meeran.navj@gmai.com  (real name; 'gmai.com' is a gmail typo —
#           migrate the identity, but the address is undeliverable: see runbook)
#       ad@nowlez.com         (company-domain account — err toward keep)
#       lolo@outlook.com      (plausible real nickname; dormant, low-cost keep)
# Err-toward-keep: dropping a real user is worse than migrating a dormant one.
_MANUAL_DROP_EMAILS = frozenset({
    "abc123@gmail.com",
    "lol1@gmail.com",
    "test1604@gmail.com",
})

# Coarse set-based pre-filter for the SQLite read (PATTERNS ONLY — the
# authoritative check is is_test_user(), which ALSO applies _MANUAL_DROP_EMAILS;
# _read_candidates re-checks in Python after this SQL cut). Invariant:
# everything this SQL excludes, is_test_user() also excludes (SQL ⊆ is_test_user).
EXCLUDE_TEST_SQL = """
    LOWER(email) LIKE '%.invalid'      OR LOWER(email) LIKE '%@example.com'
 OR LOWER(email) LIKE '%@test.com'     OR LOWER(email) LIKE '%@testx.com'
 OR LOWER(email) LIKE 'test@%'         OR LOWER(email) LIKE 'test+%'
 OR LOWER(email) LIKE '%+e2e-%'        OR LOWER(email) LIKE 'prod-test-%'
 OR LOWER(email) LIKE 'concurrent%'    OR LOWER(email) LIKE 'nonadmin%'
"""

# Post-filter guard band. The 2026-05-28 audit yields ~70 (73 pattern-clean
# minus 3 manual drops); widen generously but stay far below the ~1,658
# junk-row flood a broken denylist would admit.
EXPECTED_MIN_CANDIDATES = 40
EXPECTED_MAX_CANDIDATES = 200

# See module docstring "Field policy": currently tier carries verbatim.
_TODO_TIER_POLICY = "carry-verbatim"  # vs "free->trial" (cutover_subproject_e gesture)


def is_test_user(email: str | None) -> bool:
    """Authoritative exclusion check: True if ``email`` is a test/fixture row
    (pattern denylist) or an individually-adjudicated manual drop.

    This is a *superset* of ``EXCLUDE_TEST_SQL`` — the SQL is a coarse
    pattern-only pre-filter; the manual drops are applied here, so
    ``_read_candidates`` always re-checks in Python after the SQL cut.
    """
    if not email:
        # email is NOT NULL UNIQUE in the legacy schema, so this is defensive;
        # treat a blank as un-backfillable rather than crashing the run.
        return True
    e = email.strip().lower()
    if e in _MANUAL_DROP_EMAILS:
        return True
    return _TEST_EMAIL_RE.match(e) is not None


def assert_candidate_count(n: int) -> None:
    """Fail loud if the surviving candidate set is implausible."""
    if not (EXPECTED_MIN_CANDIDATES <= n <= EXPECTED_MAX_CANDIDATES):
        raise RuntimeError(
            f"backfill candidate count {n} outside expected band "
            f"[{EXPECTED_MIN_CANDIDATES}, {EXPECTED_MAX_CANDIDATES}] — refusing "
            f"to run; the test-row denylist may be stale or the snapshot wrong."
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _parse_dt(value) -> datetime | None:
    """Parse a legacy SQLite TEXT timestamp into a tz-aware datetime.

    Naive timestamps are assumed UTC. NOTE: if the legacy app wrote local
    (IST) naive timestamps, created_at / last_login_at land ~5.5h off. Identity
    is unaffected; only these audit timestamps. Revisit if the legacy app's
    write convention is confirmed to be local time.
    """
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            logger.warning("unparseable timestamp %r — storing NULL", value)
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _read_candidates(sqlite_path: str, *, apply_test_filter: bool) -> list[sqlite3.Row]:
    """Read legacy users from the SQLite snapshot. Read-only; closes the conn.

    When ``apply_test_filter`` is set, the coarse ``EXCLUDE_TEST_SQL`` cut runs
    in SQL, then ``is_test_user`` re-checks every surviving row in Python — this
    second pass is what applies ``_MANUAL_DROP_EMAILS`` (which the SQL omits).
    """
    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    try:
        where = f"WHERE NOT ({EXCLUDE_TEST_SQL})" if apply_test_filter else ""
        rows = conn.execute(
            "SELECT id, name, email, password_hash, created_at, last_login, "
            f"is_admin, tier, is_active FROM users {where}"
        ).fetchall()
    finally:
        conn.close()
    if apply_test_filter:
        rows = [r for r in rows if not is_test_user(r["email"])]
    return rows


def _already_linked(session: Session, legacy_id: str) -> bool:
    return session.execute(
        select(UserNowlez.user_id).where(UserNowlez.legacy_sqlite_id == legacy_id)
    ).first() is not None


def _get_user_by_email(session: Session, email: str) -> User | None:
    if not email:
        return None
    return session.execute(
        select(User).where(User.email == email)
    ).scalar_one_or_none()


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------
def backfill(
    *,
    sqlite_path: str,
    pg_session: Session,
    dry_run: bool = False,
    apply_test_filter: bool = True,
    enforce_count_band: bool | None = None,
    now: datetime | None = None,
) -> dict:
    """Back-fill legacy SQLite users into Postgres ``users`` + ``users_nowlez``.

    Args:
        sqlite_path: Path to the legacy SQLite snapshot (plan creates
            ``.local/local-prod-app.db``).
        pg_session: Open SQLAlchemy session bound to the post-migration
            Postgres DB (``users.email`` + ``users_nowlez.legacy_sqlite_id``
            columns must exist).
        dry_run: Count what *would* happen; no writes, no commit.
        apply_test_filter: Strip test/fixture rows (default True). Tests that
            want to exercise the raw insert path on a reserved-domain email
            (e.g. ``alice@example.com``) pass False.
        enforce_count_band: Assert the candidate count sits in the expected
            band. Defaults to ``apply_test_filter`` (the band assumes the
            filter ran; with the filter off the count is ~1,731).
        now: Override "now" for created_at fallbacks (tests pin this).

    Returns:
        Summary dict: candidates / inserted / already_linked /
        healed_missing_nowlez / linked_existing / email_conflict / dry_run.
    """
    now = now or datetime.now(timezone.utc)
    if enforce_count_band is None:
        enforce_count_band = apply_test_filter

    candidates = _read_candidates(sqlite_path, apply_test_filter=apply_test_filter)
    if enforce_count_band:
        assert_candidate_count(len(candidates))

    summary = {
        "candidates": len(candidates),
        "inserted": 0,
        "already_linked": 0,
        "healed_missing_nowlez": 0,
        "linked_existing": 0,
        "email_conflict": 0,
        "dry_run": dry_run,
    }

    for row in candidates:
        legacy_id = row["id"]
        email = (row["email"] or "").strip().lower()

        # Fully migrated already under this legacy id? (idempotent re-run)
        if _already_linked(pg_session, legacy_id):
            summary["already_linked"] += 1
            continue

        existing = _get_user_by_email(pg_session, email)
        if existing is not None:
            # Email already in Postgres. HEAL/LINK instead of skipping, so a
            # real user that exists without a Nowlez extension (or without a
            # legacy link) is completed rather than stranded. We never modify
            # the existing user's auth (email / phone / password).
            ext = pg_session.get(UserNowlez, existing.id)
            if ext is None:
                summary["healed_missing_nowlez"] += 1
                if not dry_run:
                    pg_session.add(UserNowlez(
                        user_id=existing.id,
                        name=row["name"],
                        is_admin=bool(row["is_admin"]),
                        tier=row["tier"],
                        legacy_sqlite_id=legacy_id,
                        created_at=_parse_dt(row["created_at"]) or now,
                    ))
                    pg_session.flush()
            elif ext.legacy_sqlite_id is None:
                summary["linked_existing"] += 1
                if not dry_run:
                    ext.legacy_sqlite_id = legacy_id
                    pg_session.flush()
            elif ext.legacy_sqlite_id == legacy_id:
                # Defensive: _already_linked should have caught this.
                summary["already_linked"] += 1
            else:
                summary["email_conflict"] += 1
                logger.warning(
                    "email %s already linked to legacy_sqlite_id=%s; refusing "
                    "to also link %s — manual reconciliation needed",
                    email, ext.legacy_sqlite_id, legacy_id,
                )
            continue

        # Net-new: create both rows under the same transaction.
        summary["inserted"] += 1
        if dry_run:
            continue
        created = _parse_dt(row["created_at"]) or now
        user = User(
            email=email,
            password_hash=row["password_hash"],  # preserves email/password login
            phone=None,                           # email-primary backfilled user
            is_active=bool(row["is_active"]),
            last_login_at=_parse_dt(row["last_login"]),
            created_at=created,
        )
        pg_session.add(user)
        pg_session.flush()  # assigns user.id

        pg_session.add(UserNowlez(
            user_id=user.id,
            name=row["name"],
            is_admin=bool(row["is_admin"]),
            tier=row["tier"],                     # TODO _TODO_TIER_POLICY
            legacy_sqlite_id=legacy_id,
            created_at=created,
        ))
        pg_session.flush()

    if not dry_run:
        pg_session.commit()

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m migrations.backfill_legacy_users",
        description=__doc__.splitlines()[0] if __doc__ else "",
    )
    parser.add_argument("--sqlite-path", required=True, help="Path to legacy SQLite snapshot.")
    parser.add_argument("--dry-run", action="store_true", help="Report counts without committing.")
    parser.add_argument(
        "--no-test-filter", action="store_true",
        help="Disable the test-row denylist (debug only; admits fixtures AND "
             "disables the candidate-count band).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from data_access import get_session

    with get_session() as session:
        summary = backfill(
            sqlite_path=args.sqlite_path,
            pg_session=session,
            dry_run=args.dry_run,
            apply_test_filter=not args.no_test_filter,
        )

    logger.info("backfill_legacy_users summary: %s", summary)
    return 0


if __name__ == "__main__":  # pragma: no cover - script entrypoint
    sys.exit(main())
