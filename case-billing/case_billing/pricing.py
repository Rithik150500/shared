"""Pricing matrix calculator (spec Section 1.5).

Centralizes the Nowlez tier price and referral-mutual-benefit discount so
the rest of the codebase never has to hard-code rupee amounts. All prices
are in **paise** (1 INR = 100 paise); the database and Razorpay both expect
paise, so the conversion happens exactly once at the user-facing UI
boundary.

ONE SELLABLE PLAN (2026-08-10) — pricing matrix (per cycle, all in INR):

    +-----------+--------+--------+--------+
    | tier      | cycle1 | cycle2 | cycle3+|
    +-----------+--------+--------+--------+
    | chambers  | 1,000  | 1,000  | 1,000  |
    |           |        | 500R   | 1,000  |
    +-----------+--------+--------+--------+
        R = referred — referree got cycle-2 half off

``advocate`` and ``counsel`` are retired and deliberately absent from
:data:`TIER_PRICES_PAISE` — a renewal for such a row raises ``KeyError``
rather than silently billing an invented amount.

The half-price intro promo on cycle 1 was DROPPED by owner decision on
2026-08-10: with a 30-day free trial in front of a ₹1,000 plan, a further
discount ladder on month one bought nothing.
:func:`calculate_first_payment_paise` now always returns list price.

This is the PRE-GST rupee value. What Razorpay actually collects is set by
the plan there, which the 2026-08-10 repricing deliberately did not touch.

Spec FAQ Q9's advocate-exclusion-from-referral-mutual rule is preserved in
:func:`is_advocate_excluded_from_referral_mutual` even though advocate is no
longer sellable — the key still exists system-wide on surviving rows.
"""

from __future__ import annotations

import uuid
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session


# --- Constants --------------------------------------------------------------

# Full per-cycle list prices, in paise. ONE sellable plan as of 2026-08-10;
# `counsel` and `advocate` are retired and deliberately absent — a renewal for
# such a row raises KeyError rather than silently billing an invented amount.
#
# This is the PRE-GST rupee value. What Razorpay actually collects is set by
# the plan there, which the 2026-08-10 repricing deliberately did not touch.
TIER_PRICES_PAISE: Mapping[str, int] = {
    "chambers": 100_000,   # ₹1,000
}

# Price charged on cycle 2 when the referree has a pending mutual benefit:
# half a month. The referral programme survived the repricing.
REFERRER_MUTUAL_PAISE: Mapping[str, int] = {
    "chambers": 50_000,    # half of ₹1,000
}

# Intro-promo states that *consume* the lifetime-once promo (the user has
# either entered intro pricing or completed it). 'pre_first_payment' and
# 'skipped' do NOT consume the promo.
_INTRO_PROMO_CONSUMED_STATES: frozenset[str] = frozenset({"in_intro", "past_intro"})


# --- Cycle 1 (first paid month after trial) ---------------------------------


def calculate_first_payment_paise(tier: str) -> int:
    """Return the cycle-1 charge in paise for a brand-new subscriber.

    List price. The half-price intro promo was RETIRED on 2026-08-10: with a
    30-day free trial in front of a ₹1,000 plan, a further discount ladder on
    month one bought nothing. Do not reintroduce one here without also changing
    the tier picker, which promises exactly what this function charges.

    Args:
        tier: Must be ``"chambers"``.

    Raises:
        ValueError: If ``tier`` is not a sellable tier.
    """
    if tier not in TIER_PRICES_PAISE:
        raise ValueError(f"Unknown or retired tier: {tier!r}")
    return TIER_PRICES_PAISE[tier]


# --- Cycle 2 onwards --------------------------------------------------------


def calculate_renewal_price_paise(
    tier: str,
    cycle_number: int,
    has_pending_referral_mutual: bool,
) -> int:
    """Return the per-cycle charge in paise for cycles 2 and later.

    Args:
        tier: Must be ``"chambers"`` — the only sellable tier. A retired
            tier (``"advocate"``, ``"counsel"``) is not in
            :data:`TIER_PRICES_PAISE`, so the lookup below raises
            ``KeyError`` rather than silently inventing a renewal price.
        cycle_number: The billing cycle index (2 = first renewal). Cycle 1
            is rejected — callers should route first payments through
            :func:`calculate_first_payment_paise` to surface that decision
            explicitly.
        has_pending_referral_mutual: True iff the referree-side cycle-2
            mutual discount is unredeemed at the time this cycle bills.

    Cycle 2 with a pending referral mutual benefit charges half price;
    cycle 3+ always charges full price (the mutual benefit is a one-shot,
    not a permanent discount).

    Raises:
        ValueError: If ``cycle_number`` < 2.
        KeyError: If ``tier`` is not a recognised (sellable) tier name.
    """
    if cycle_number < 2:
        raise ValueError(
            f"calculate_renewal_price_paise only handles cycle 2+; got {cycle_number}. "
            "Route cycle 1 through calculate_first_payment_paise."
        )
    base = TIER_PRICES_PAISE[tier]
    if cycle_number == 2 and has_pending_referral_mutual:
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
