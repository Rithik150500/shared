"""Sub-project E cutover migration — run AFTER alembic ``20260601_e_billing``.

This script bridges the launch-day gap between the new sub-project E billing
schema (alembic migration ``20260601_subproject_e_billing.py``) and the legacy
state held on ``users_nowlez`` and the deprecated ``subscriptions`` shape.

Three idempotent steps, in this order (Task 17.1.1):

1. **Backfill ``subscriptions``** for each ``users_nowlez.razorpay_customer_id
   IS NOT NULL`` user. We INSERT a row mirroring the user's prior Razorpay
   subscription state and set the new state-machine columns conservatively:

   * ``intro_promo_state='past_intro'`` — every existing paid customer has
     already consumed their first-month welcome promo (or never had one,
     for Advocate). Per plan §17.1.1 we assume "already consumed".
   * ``referral_state='mutual_applied'`` when ``users_nowlez.referred_by IS
     NOT NULL`` and the referral state machine has already run, else
     ``'no_referral'``.

   **Plan deviation re: ``referral_state``.** The plan's task description
   lists ``'past_referral'`` as the post-consumption value, but Round 4
   commit ``1357ed8`` narrowed the SQL CHECK constraint on
   ``subscriptions.referral_state`` to ``('no_referral', 'pending_mutual',
   'mutual_applied', 'expired')`` (NO ``'past_referral'``). We use
   ``'mutual_applied'`` for the "already-consumed" case so the INSERT
   survives the CHECK; documented here so the discrepancy is auditable
   when ops re-reads the cutover later.

2. **Reset legacy free-tier signups to the new trial flow.** Every
   ``users_nowlez.tier='free'`` row is rewritten to ``tier=NULL`` and
   given a fresh 30-day Chambers trial (``trial_started_at=NOW()``,
   ``trial_ends_at=NOW()+30 days``). This is the spec §5.7 "gesture":
   existing free users get the same on-ramp as new signups instead of
   being orphaned by the pricing change.

3. **Verify ``users_munshi.billing_anniversary_date`` is populated** for
   every existing Munshi user — the alembic migration backfills
   ``= created_at::date`` on Postgres before flipping the column to NOT
   NULL. We sanity-check the invariant here so a botched alembic run
   surfaces loudly (and is the canary the dry-run mode runs in CI).

Idempotency (Task 17.1.3): the script is safe to re-run. Every step
checks for "already done" state before mutating:

* The ``subscriptions`` backfill uses a per-user existence check on
  ``razorpay_subscription_id`` (NULL → skip; if already a row exists
  with the user's ``razorpay_subscription_id`` we skip).
* The free-tier rewrite checks ``tier='free'`` (already None → skip).
* The Munshi verification is a pure read — never writes.

Entry points:

    python -m migrations.cutover_subproject_e            # default: live run
    python -m migrations.cutover_subproject_e --dry-run  # count + report only

The dry-run mode prints the counts that *would* be touched and exits
without committing — used as the pre-launch sanity check.

Coordination notes:

* Run **after** ``alembic upgrade head`` so the schema is in place.
* Run **before** flipping ``MUNSHI_BILLING_ENABLED=true`` so the cron's
  cross-product gate sees the new ``users_nowlez.tier=NULL`` state.
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)


# Spec §1.1 / Task 7.1: every Nowlez signup gets 30 days of Chambers trial.
# We mirror that here for legacy `tier='free'` users so they land on the
# same on-ramp the new signup flow uses.
NOWLEZ_TRIAL_DURATION_DAYS: int = 30

# Plan §17.1.1 mandates ``intro_promo_state='past_intro'`` for backfilled
# subscriptions: existing paid customers have already consumed (or never
# qualified for) the welcome offer. The value matches the CHECK
# constraint on ``subscriptions.intro_promo_state`` from migration
# ``20260601_e_billing``.
INTRO_PROMO_STATE_BACKFILL: str = "past_intro"

# Plan §17.1.1 calls for ``'past_referral'`` when ``referred_by IS NOT
# NULL`` but the SQL CHECK constraint (migration ``20260601_e_billing``)
# only allows ``('no_referral','pending_mutual','mutual_applied',
# 'expired')``. We pick ``'mutual_applied'`` to represent "the referral
# was used and the discount has already been granted" — the closest
# spec-conformant analogue. Documented in the module docstring above.
REFERRAL_STATE_HAS_REFERRER: str = "mutual_applied"
REFERRAL_STATE_NO_REFERRER: str = "no_referral"


# ---------------------------------------------------------------------------
# Step 1 — subscriptions backfill
# ---------------------------------------------------------------------------


def _backfill_subscriptions(
    session: Session,
    *,
    now: datetime,
    dry_run: bool,
) -> int:
    """INSERT one ``subscriptions`` row per legacy Razorpay-paying user.

    Returns the number of rows inserted (or that *would* be inserted in
    dry-run mode).

    Idempotency: a per-user lookup on ``razorpay_subscription_id`` skips
    users we've already processed. Where the legacy ``razorpay_subscription_id``
    is NULL (Munshi-only / never-subscribed) we skip entirely — there's
    nothing to mirror.
    """
    # Read every legacy Razorpay user. We pull a small set of columns to
    # keep the query cheap on a large `users_nowlez`.
    legacy_rows = session.execute(text(
        """
        SELECT user_id, tier, referred_by,
               razorpay_customer_id, razorpay_subscription_id
          FROM users_nowlez
         WHERE razorpay_customer_id IS NOT NULL
        """
    )).mappings().all()

    inserted = 0
    for row in legacy_rows:
        user_id = row["user_id"]
        rzp_sub_id = row["razorpay_subscription_id"]

        # Idempotency: skip if we already inserted a row for this
        # Razorpay subscription id (UNIQUE on subscriptions.razorpay_subscription_id).
        if rzp_sub_id:
            existing = session.execute(
                text(
                    "SELECT 1 FROM subscriptions "
                    "WHERE razorpay_subscription_id = :rid LIMIT 1"
                ),
                {"rid": rzp_sub_id},
            ).first()
            if existing is not None:
                continue
        else:
            # No legacy subscription id; we can't dedup on it. Fall back
            # to a per-user existence check on `user_id` to keep the run
            # idempotent. (A user could have multiple subscriptions but
            # at cutover time each legacy user has at most one active
            # row in the deprecated `subscriptions` shape.)
            existing = session.execute(
                text(
                    "SELECT 1 FROM subscriptions WHERE user_id = :uid LIMIT 1"
                ),
                {"uid": str(user_id)},
            ).first()
            if existing is not None:
                continue

        tier = row["tier"]
        # `subscriptions.tier` CHECK only allows
        # ('advocate','counsel','chambers'). If the legacy row had
        # 'free' (no real subscription) we skip — those users are
        # handled by step 2's trial reset.
        if tier not in ("advocate", "counsel", "chambers"):
            continue

        referral_state = (
            REFERRAL_STATE_HAS_REFERRER
            if row["referred_by"] is not None
            else REFERRAL_STATE_NO_REFERRER
        )

        if dry_run:
            inserted += 1
            continue

        # Mirror the legacy state. We mark status='active' because
        # `razorpay_customer_id IS NOT NULL` implies a paying customer;
        # webhook handlers will reconcile if Razorpay reports otherwise.
        #
        # We explicitly supply the PK as a Python-side ``uuid4`` rather
        # than relying on ``DEFAULT gen_random_uuid()`` because the
        # Postgres-only server_default doesn't exist on SQLite — and
        # this script is exercised on both dialects (SQLite for the
        # test path, Postgres for the live cutover).
        session.execute(
            text(
                """
                INSERT INTO subscriptions (
                    id, user_id, tier, billing_cycle,
                    razorpay_customer_id, razorpay_subscription_id,
                    status, intro_promo_state, referral_state,
                    created_at, updated_at
                ) VALUES (
                    :id, :user_id, :tier, 'monthly',
                    :customer_id, :sub_id,
                    'active', :intro_state, :referral_state,
                    :now, :now
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "user_id": str(user_id),
                "tier": tier,
                "customer_id": row["razorpay_customer_id"],
                "sub_id": rzp_sub_id,
                "intro_state": INTRO_PROMO_STATE_BACKFILL,
                "referral_state": referral_state,
                "now": now,
            },
        )
        inserted += 1

    return inserted


# ---------------------------------------------------------------------------
# Step 2 — reset legacy `tier='free'` to trial flow
# ---------------------------------------------------------------------------


def _reset_free_tier_to_trial(
    session: Session,
    *,
    now: datetime,
    dry_run: bool,
) -> int:
    """Rewrite ``tier='free'`` rows to ``tier=NULL`` + 30-day trial window.

    Returns the count of rows touched. Idempotent: only rows still
    matching ``tier='free'`` are eligible, so a second run finds none.
    """
    # Count first so dry-run and live runs share the same query plan.
    count = session.execute(text(
        "SELECT COUNT(*) FROM users_nowlez WHERE tier = 'free'"
    )).scalar_one()

    if dry_run or count == 0:
        return int(count)

    trial_ends_at = now + timedelta(days=NOWLEZ_TRIAL_DURATION_DAYS)
    session.execute(
        text(
            """
            UPDATE users_nowlez
               SET tier = NULL,
                   trial_started_at = :now,
                   trial_ends_at = :ends_at
             WHERE tier = 'free'
            """
        ),
        {"now": now, "ends_at": trial_ends_at},
    )
    return int(count)


# ---------------------------------------------------------------------------
# Step 3 — verify users_munshi.billing_anniversary_date populated
# ---------------------------------------------------------------------------


def _verify_munshi_anniversary(session: Session) -> int:
    """Sanity-check the alembic backfill ran on ``users_munshi``.

    Returns the count of Munshi rows still missing
    ``billing_anniversary_date``. A non-zero return is a hard error and
    the cutover refuses to continue: shipping crons that match against
    NULL anniversary days silently drops users.
    """
    missing = session.execute(text(
        "SELECT COUNT(*) FROM users_munshi "
        "WHERE billing_anniversary_date IS NULL"
    )).scalar_one()
    return int(missing)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_cutover(
    session: Session,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute the three-step cutover; return a summary dict.

    Args:
        session: Open SQLAlchemy session bound to the post-alembic DB.
        now: Optional override for the timestamp written into new
            subscriptions / trial windows. Tests pin this; production
            uses ``datetime.now(timezone.utc)``.
        dry_run: When True, no mutations are performed; the returned
            counts describe what *would* be done.

    Returns:
        ``{"subscriptions_inserted": int, "free_users_reset": int,
            "munshi_missing_anniversary": int, "dry_run": bool}``.

    Raises:
        RuntimeError: if step 3 reports any Munshi user missing a
            ``billing_anniversary_date``. The cutover refuses to commit
            so ops can investigate.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    subscriptions_inserted = _backfill_subscriptions(
        session, now=now, dry_run=dry_run,
    )
    free_users_reset = _reset_free_tier_to_trial(
        session, now=now, dry_run=dry_run,
    )
    munshi_missing = _verify_munshi_anniversary(session)

    if munshi_missing > 0:
        # Surface loudly. The alembic migration is supposed to flip the
        # column to NOT NULL after backfilling; if we got here, either
        # alembic wasn't run, the backfill failed, or someone INSERTed
        # a Munshi user between alembic and this cutover.
        msg = (
            f"users_munshi.billing_anniversary_date is NULL for "
            f"{munshi_missing} row(s) — alembic backfill did not complete"
        )
        if not dry_run:
            session.rollback()
            raise RuntimeError(msg)
        logger.warning("DRY-RUN: %s", msg)

    if not dry_run:
        session.commit()

    return {
        "subscriptions_inserted": subscriptions_inserted,
        "free_users_reset": free_users_reset,
        "munshi_missing_anniversary": munshi_missing,
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m migrations.cutover_subproject_e",
        description=__doc__.splitlines()[0] if __doc__ else "",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report counts without committing.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from data_access import get_session

    @contextmanager
    def _session_scope():
        with get_session() as s:  # type: ignore[misc]
            yield s

    with _session_scope() as session:
        summary = run_cutover(session, dry_run=args.dry_run)

    logger.info("cutover_subproject_e summary: %s", summary)
    return 0


if __name__ == "__main__":  # pragma: no cover - script entrypoint
    sys.exit(main())
