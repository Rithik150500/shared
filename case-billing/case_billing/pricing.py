"""Pricing matrix calculator (spec Section 1.5).

Centralizes every Nowlez tier price, intro-promo half-off rule, and
referral-mutual-benefit discount so the rest of the codebase never has to
hard-code rupee amounts. All prices are in **paise** (1 INR = 100 paise);
the database and Razorpay both expect paise, so the conversion happens
exactly once at the user-facing UI boundary.

Spec Section 1.5 — pricing matrix (per cycle, all in INR):

    +-----------+--------+--------+--------+
    | tier      | cycle1 | cycle2 | cycle3+|
    +-----------+--------+--------+--------+
    | chambers  | 2,000* | 4,000  | 4,000  |
    |           |        | 2,000R | 4,000  |
    +-----------+--------+--------+--------+
    | counsel   | 1,000* | 2,000  | 2,000  |
    |           |        | 1,000R | 2,000  |
    +-----------+--------+--------+--------+
    | advocate  | 1,000  | 1,000  | 1,000  |
    |           |        | 1,000R | 1,000  |
    +-----------+--------+--------+--------+
        * = intro promo half-price (lifetime-once)
        R = referred — referree got cycle-2 half off

Spec FAQ Q9: Advocate is *excluded* from referral mutual benefit — neither
the referrer nor the referree side earns a discount when either is on
the advocate tier. See :func:`is_advocate_excluded_from_referral_mutual`.

The intro promo is lifetime-once per user (anti-abuse, spec sub-project C):
:func:`is_eligible_for_intro_promo` returns False if any prior subscription
row for the user has ``intro_promo_state IN ('in_intro','past_intro')``.
"""

from __future__ import annotations

import uuid
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session


# --- Constants --------------------------------------------------------------

# Full per-cycle list prices, in paise.
TIER_PRICES_PAISE: Mapping[str, int] = {
    "advocate": 100_000,   # ₹1,000
    "counsel":  200_000,   # ₹2,000
    "chambers": 400_000,   # ₹4,000
}

# Price actually charged on cycle 2 when the referree has a pending mutual
# benefit. Advocate keeps its full price because the tier is excluded from
# the mutual-benefit programme entirely (spec FAQ Q9).
REFERRER_MUTUAL_PAISE: Mapping[str, int] = {
    "advocate": 100_000,   # no discount — advocate excluded per Q9
    "counsel":  100_000,   # half of ₹2,000
    "chambers": 200_000,   # half of ₹4,000
}

# Intro-promo states that *consume* the lifetime-once promo (the user has
# either entered intro pricing or completed it). 'pre_first_payment' and
# 'skipped' do NOT consume the promo.
_INTRO_PROMO_CONSUMED_STATES: frozenset[str] = frozenset({"in_intro", "past_intro"})


# --- Cycle 1 (first paid month after trial) ---------------------------------


def calculate_first_payment_paise(tier: str) -> int:
    """Return the cycle-1 charge in paise for a brand-new subscriber.

    Chambers and Counsel get the half-price intro promo on first payment;
    Advocate pays its full ₹1,000 (advocate has no intro promo because the
    list price is already at the floor).

    Args:
        tier: One of ``"advocate"``, ``"counsel"``, ``"chambers"``.

    Raises:
        ValueError: If ``tier`` is not a recognised tier name.
    """
    if tier == "chambers":
        return 200_000  # ₹2,000 = half of ₹4,000
    if tier == "counsel":
        return 100_000  # ₹1,000 = half of ₹2,000
    if tier == "advocate":
        return 100_000  # full ₹1,000 (no intro promo)
    raise ValueError(f"Unknown tier: {tier!r}")


# --- Cycle 2 onwards --------------------------------------------------------


def calculate_renewal_price_paise(
    tier: str,
    cycle_number: int,
    has_pending_referral_mutual: bool,
) -> int:
    """Return the per-cycle charge in paise for cycles 2 and later.

    Args:
        tier: One of ``"advocate"``, ``"counsel"``, ``"chambers"``.
        cycle_number: The billing cycle index (2 = first renewal). Cycle 1
            is rejected — callers should route first payments through
            :func:`calculate_first_payment_paise` to surface the intro
            promo decision explicitly.
        has_pending_referral_mutual: True iff the referree-side cycle-2
            mutual discount is unredeemed at the time this cycle bills.

    Cycle 2 with a pending referral mutual benefit charges half price for
    Counsel/Chambers; cycle 3+ always charges full price (the mutual
    benefit is a one-shot, not a permanent discount). Advocate ignores the
    flag entirely because advocate is excluded per spec Q9.

    Raises:
        ValueError: If ``cycle_number`` < 2.
        KeyError: If ``tier`` is not a recognised tier name.
    """
    if cycle_number < 2:
        raise ValueError(
            f"calculate_renewal_price_paise only handles cycle 2+; got {cycle_number}. "
            "Route cycle 1 through calculate_first_payment_paise."
        )
    base = TIER_PRICES_PAISE[tier]
    if (
        cycle_number == 2
        and has_pending_referral_mutual
        and tier != "advocate"
    ):
        return base // 2
    return base


# --- Lifetime-once intro promo eligibility ----------------------------------


def is_eligible_for_intro_promo(user_id: uuid.UUID, session: Session) -> bool:
    """Return True iff this user has never consumed the intro promo.

    Implements the lifetime-once anti-abuse rule from sub-project C: a user
    who has *ever* held a subscription whose ``intro_promo_state`` was
    ``'in_intro'`` or ``'past_intro'`` is permanently ineligible. Prior
    rows in ``'pre_first_payment'`` (cancelled before billing) or
    ``'skipped'`` (e.g. advocate tier where the promo never applied) do
    not count as consumption.

    Args:
        user_id: UUID of the user to check.
        session: Open SQLAlchemy Session bound to the billing DB.
    """
    # Imported lazily so importing case_billing.pricing does not transitively
    # pull SQLAlchemy ORM mappers into every consumer (Razorpay-only call sites
    # for instance pay no SQL boot cost).
    from data_access.models.billing import Subscription

    stmt = (
        select(Subscription.id)
        .where(Subscription.user_id == user_id)
        .where(Subscription.intro_promo_state.in_(_INTRO_PROMO_CONSUMED_STATES))
        .limit(1)
    )
    return session.execute(stmt).first() is None


# --- Advocate-exclusion rule (spec FAQ Q9) ----------------------------------


def is_advocate_excluded_from_referral_mutual(
    referrer_tier: str,
    referred_tier: str,
) -> bool:
    """Return True iff this referral pair is excluded from the mutual benefit.

    Per spec FAQ Q9: when *either* side is on the advocate tier, neither
    side earns the referral mutual discount. Both sides are blocked
    together so the rule cannot be gamed by a same-tier swap.
    """
    return "advocate" in (referrer_tier, referred_tier)
