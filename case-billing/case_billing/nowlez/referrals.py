"""Referral state machine for Nowlez subscriptions (Task 7.3).

Two interacting tables hold referral state:

* ``referrals.state`` — covers the referrer side:
      pending → mutual_applied (referrer earned a cycle-2 discount)
      pending → expired         (referree never paid before cutoff)

* ``subscriptions.referral_state`` — covers the referree side:
      no_referral → pending_mutual (referral attached at subscribe time)
      pending_mutual → mutual_applied (cycle-2 half-off Razorpay offer
                                       attached to the next invoice)
      pending_mutual → expired (timeout)

Plus the **Advocate exclusion** from spec FAQ Q9: when *either* side
holds the advocate tier, neither side earns the discount. This module
encapsulates the exclusion logic so callers don't have to repeat the
tier check.

Public surface:

* :func:`find_or_create_referral` — idempotent lookup that attaches a
  referrer's code to a new signup; returns the (possibly new) Referral
  row or None on validation failure (self-referral or unknown code).
* :func:`apply_referree_2nd_month_discount` — invoked when the
  referree's cycle-2 invoice is about to be issued; attaches the
  half-price Razorpay offer (caller pre-fetches the offer id) and
  transitions ``subscriptions.referral_state`` to ``mutual_applied``.
  Skips the transition when the referree is on the advocate tier.
* :func:`schedule_referrer_mutual_benefit` — invoked once the referree
  pays their first cycle; attaches the mutual-benefit offer to the
  referrer's next invoice. Skips when *either* side is advocate.
* :func:`mark_referrer_mutual_applied` — transitions the referrer-side
  ``referrals.state`` to ``mutual_applied`` after the discounted cycle
  successfully bills.
* :func:`expire_referral` — admin / cron-driven path to mark a stale
  referral ``expired`` (e.g. referree never paid within 60 days).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from case_billing.errors import BillingError
from case_billing.pricing import is_advocate_excluded_from_referral_mutual


def _tier_of_user(session: Session, user_id: uuid.UUID) -> str | None:
    """Return ``users_nowlez.tier`` for the given user, or None."""
    from data_access.models.user import UserNowlez

    return session.execute(
        select(UserNowlez.tier).where(UserNowlez.user_id == user_id)
    ).scalar_one_or_none()


async def find_or_create_referral(
    *,
    referred_user_id: uuid.UUID,
    referral_code: str,
    session: Session,
) -> Any:
    """Attach a referrer's code to a new signup; return the Referral row.

    Returns None when:
      * No ``users_nowlez.referral_code`` matches.
      * The referrer is the same user (self-referral guard).

    The function is idempotent — calling twice for the same referree
    returns the same row (the UNIQUE constraint on
    ``referrals.referred_user_id`` ensures only one row can exist
    anyway; this code path makes the no-op explicit).
    """
    from data_access.models.billing import Referral
    from data_access.models.user import UserNowlez

    # Look up the referrer by code.
    referrer_id = session.execute(
        select(UserNowlez.user_id).where(UserNowlez.referral_code == referral_code)
    ).scalar_one_or_none()
    if referrer_id is None:
        return None
    # SQLite drops the UUID type info on round-trip; compare on string form
    # so self-referral detection works on both SQLite and Postgres.
    if str(referrer_id) == str(referred_user_id):
        return None

    # Existing row? Idempotent path.
    existing = session.execute(
        select(Referral).where(Referral.referred_user_id == referred_user_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    referral = Referral(
        referrer_user_id=referrer_id,
        referred_user_id=referred_user_id,
        state="pending",
    )
    session.add(referral)
    session.flush()
    return referral


async def apply_referree_2nd_month_discount(
    subscription_id: uuid.UUID,
    session: Session,
) -> bool:
    """Apply the cycle-2 half-off discount to the referree's subscription.

    Returns True iff the discount was actually applied (state moved to
    ``mutual_applied``). Returns False when:

    * The subscription is missing or not in ``pending_mutual``.
    * The referree is on the advocate tier (advocate exclusion).

    The Razorpay offer attachment itself is the caller's responsibility
    — this module only manages the local state; the caller wires the
    offer via ``case_billing.razorpay_client.subscriptions.update_subscription_offer``
    before invoking this function (so a Razorpay failure leaves the
    local state untouched).
    """
    from data_access.models.billing import Referral, Subscription

    sub = session.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    ).scalar_one_or_none()
    if sub is None:
        return False
    if sub.referral_state != "pending_mutual":
        return False
    if sub.tier == "advocate":
        # Advocate is excluded — even if a referral exists, the discount
        # does not apply. We leave state in pending_mutual rather than
        # transitioning to expired so admin tooling can audit later.
        return False

    sub.referral_state = "mutual_applied"
    session.flush()
    return True


async def schedule_referrer_mutual_benefit(
    referral_id: uuid.UUID,
    session: Session,
) -> bool:
    """Attach the mutual-benefit offer to the referrer's next invoice.

    Returns True iff the schedule actually fired. Skips when either
    side of the referral is on the advocate tier (spec FAQ Q9).

    Like :func:`apply_referree_2nd_month_discount`, this module only
    manages the local state; the Razorpay offer attachment is the
    caller's responsibility (so a Razorpay failure can be retried
    without re-running our local idempotency check).
    """
    from data_access.models.billing import Referral

    referral = session.execute(
        select(Referral).where(Referral.id == referral_id)
    ).scalar_one_or_none()
    if referral is None:
        return False
    if referral.state != "pending":
        return False

    referrer_tier = _tier_of_user(session, referral.referrer_user_id) or "advocate"
    referred_tier = _tier_of_user(session, referral.referred_user_id) or "advocate"
    if is_advocate_excluded_from_referral_mutual(referrer_tier, referred_tier):
        # Either side advocate → neither earns mutual benefit. State
        # stays in 'pending' so admin tooling can see why.
        return False

    # Note: we don't transition to 'mutual_applied' here because the
    # actual application happens after Razorpay confirms the cycle
    # billed at the discounted price. mark_referrer_mutual_applied is
    # the terminal transition.
    return True


async def mark_referrer_mutual_applied(
    referral_id: uuid.UUID,
    session: Session,
) -> None:
    """Transition a Referral row to ``state='mutual_applied'``.

    Called after the referrer's discounted cycle bills successfully so
    the referrer earned their mutual benefit. Idempotent — a second
    call when already in mutual_applied is a no-op.
    """
    from data_access.models.billing import Referral

    referral = session.execute(
        select(Referral).where(Referral.id == referral_id)
    ).scalar_one_or_none()
    if referral is None:
        return
    if referral.state == "mutual_applied":
        return
    if referral.state != "pending":
        raise BillingError(
            f"mark_referrer_mutual_applied: referral {referral_id} in unexpected "
            f"state {referral.state!r}"
        )
    referral.state = "mutual_applied"
    session.flush()


async def expire_referral(
    referral_id: uuid.UUID,
    session: Session,
) -> None:
    """Mark a stale referral ``expired`` (cron / admin path).

    Idempotent: no-op when the referral is missing or already
    terminal (``mutual_applied`` or ``expired``).
    """
    from data_access.models.billing import Referral

    referral = session.execute(
        select(Referral).where(Referral.id == referral_id)
    ).scalar_one_or_none()
    if referral is None:
        return
    if referral.state in ("mutual_applied", "expired"):
        return
    referral.state = "expired"
    session.flush()


# Plan 7.3.1 also references `apply_referral_at_first_payment` as a
# state transition that bridges find_or_create_referral and the
# discount application. With the existing referrals schema we can't
# record `referred_tier`/`referred_first_payment_at` on the row, but
# the caller can flip `subscriptions.referral_state = 'pending_mutual'`
# at subscription creation (Task 7.4) so this function is a thin
# wrapper for clarity.
async def apply_referral_at_first_payment(
    *,
    referral_id: uuid.UUID,
    referred_subscription_id: uuid.UUID,
    session: Session,
) -> None:
    """Wire a referral to its referree's subscription at first payment.

    Updates the Referral row's ``referred_subscription_id`` so reporting
    queries can join the two without a lookup. No state transition
    happens here — :func:`apply_referree_2nd_month_discount` and
    :func:`schedule_referrer_mutual_benefit` are responsible for moves.
    """
    from data_access.models.billing import Referral

    referral = session.execute(
        select(Referral).where(Referral.id == referral_id)
    ).scalar_one_or_none()
    if referral is None:
        return
    referral.referred_subscription_id = referred_subscription_id
    session.flush()
