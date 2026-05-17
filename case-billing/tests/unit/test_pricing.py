"""Pricing matrix unit tests (Task 5.2).

Covers every row of spec Section 1.5 plus the intro-promo lifetime-once
check and the advocate-exclusion rule (Q9 of the spec FAQ).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from case_billing.pricing import (
    REFERRER_MUTUAL_PAISE,
    TIER_PRICES_PAISE,
    calculate_first_payment_paise,
    calculate_renewal_price_paise,
    is_advocate_excluded_from_referral_mutual,
    is_eligible_for_intro_promo,
)


# --- 5.1 module constants ---------------------------------------------------


def test_tier_prices_are_in_paise_matching_spec() -> None:
    """Section 1.5 prices, expressed in paise (1 INR = 100 paise)."""
    assert TIER_PRICES_PAISE["advocate"] == 100_000  # ₹1,000
    assert TIER_PRICES_PAISE["counsel"] == 200_000  # ₹2,000
    assert TIER_PRICES_PAISE["chambers"] == 400_000  # ₹4,000


def test_referrer_mutual_paise_matches_spec() -> None:
    """Mutual-benefit cycle-2 discounts; advocate is full price (no discount)."""
    assert REFERRER_MUTUAL_PAISE["advocate"] == 100_000
    assert REFERRER_MUTUAL_PAISE["counsel"] == 100_000  # half off ₹2k
    assert REFERRER_MUTUAL_PAISE["chambers"] == 200_000  # half off ₹4k


# --- 5.1.1 calculate_first_payment_paise (cycle 1 / intro promo) ------------


@pytest.mark.parametrize(
    "tier,expected_paise",
    [
        ("chambers", 200_000),  # half of ₹4,000 — intro promo
        ("counsel", 100_000),   # half of ₹2,000 — intro promo
        ("advocate", 100_000),  # full price — no intro promo
    ],
)
def test_calculate_first_payment_paise(tier: str, expected_paise: int) -> None:
    assert calculate_first_payment_paise(tier) == expected_paise


def test_calculate_first_payment_paise_rejects_unknown_tier() -> None:
    with pytest.raises(ValueError, match="Unknown tier"):
        calculate_first_payment_paise("enterprise")


# --- 5.1.1 calculate_renewal_price_paise (cycle 2+) -------------------------


@pytest.mark.parametrize(
    "tier,cycle,has_referral,expected",
    [
        # Chambers — Section 1.5 row 1
        ("chambers", 2, False, 400_000),
        ("chambers", 2, True, 200_000),
        ("chambers", 3, False, 400_000),
        ("chambers", 3, True, 400_000),
        ("chambers", 6, False, 400_000),
        # Counsel — Section 1.5 row 2
        ("counsel", 2, False, 200_000),
        ("counsel", 2, True, 100_000),
        ("counsel", 3, False, 200_000),
        ("counsel", 3, True, 200_000),
        # Advocate — Section 1.5 row 3 (no discount ever, per Q9)
        ("advocate", 2, False, 100_000),
        ("advocate", 2, True, 100_000),
        ("advocate", 3, False, 100_000),
        ("advocate", 3, True, 100_000),
    ],
)
def test_calculate_renewal_price_paise(
    tier: str, cycle: int, has_referral: bool, expected: int
) -> None:
    assert calculate_renewal_price_paise(tier, cycle, has_referral) == expected


def test_calculate_renewal_price_paise_rejects_cycle_one() -> None:
    """Cycle 1 must go through calculate_first_payment_paise; renewal only handles 2+."""
    with pytest.raises(ValueError):
        calculate_renewal_price_paise("chambers", cycle_number=1, has_pending_referral_mutual=False)


def test_calculate_renewal_price_paise_rejects_unknown_tier() -> None:
    with pytest.raises(KeyError):
        calculate_renewal_price_paise("enterprise", cycle_number=2, has_pending_referral_mutual=False)


# --- 5.1.3 is_advocate_excluded_from_referral_mutual ------------------------


@pytest.mark.parametrize(
    "referrer,referred,expected",
    [
        # Either side advocate → excluded.
        ("advocate", "advocate", True),
        ("advocate", "counsel", True),
        ("advocate", "chambers", True),
        ("counsel", "advocate", True),
        ("chambers", "advocate", True),
        # No advocate involved → both sides earn mutual benefit.
        ("counsel", "counsel", False),
        ("counsel", "chambers", False),
        ("chambers", "counsel", False),
        ("chambers", "chambers", False),
    ],
)
def test_is_advocate_excluded_from_referral_mutual(
    referrer: str, referred: str, expected: bool
) -> None:
    assert is_advocate_excluded_from_referral_mutual(referrer, referred) is expected


# --- 5.1.2 is_eligible_for_intro_promo (lifetime-once enforcement) ----------


@pytest.fixture()
def session() -> Session:
    """In-memory SQLite session with the billing schema applied."""
    from data_access.base import Base

    # Importing billing registers the Subscription mapper on Base.metadata.
    # (The Subscription FK to users requires the users table too, so import auth.)
    import data_access.models.auth  # noqa: F401
    import data_access.models.billing  # noqa: F401
    import data_access.models.user  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _new_user_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_subscription(
    session: Session,
    *,
    user_id: uuid.UUID,
    tier: str = "chambers",
    intro_promo_state: str,
    status: str = "active",
) -> None:
    from data_access.models.billing import Subscription

    sub = Subscription(
        user_id=user_id,
        tier=tier,
        billing_cycle="monthly",
        status=status,
        intro_promo_state=intro_promo_state,
        referral_state="no_referral",
        period_start=datetime.now(timezone.utc),
        period_end=datetime.now(timezone.utc),
    )
    session.add(sub)
    session.flush()


def test_is_eligible_for_intro_promo_true_for_user_with_no_subscriptions(
    session: Session,
) -> None:
    assert is_eligible_for_intro_promo(_new_user_id(), session) is True


def test_is_eligible_for_intro_promo_false_when_in_intro(session: Session) -> None:
    user_id = _new_user_id()
    _make_subscription(session, user_id=user_id, intro_promo_state="in_intro")
    assert is_eligible_for_intro_promo(user_id, session) is False


def test_is_eligible_for_intro_promo_false_when_past_intro(session: Session) -> None:
    user_id = _new_user_id()
    _make_subscription(session, user_id=user_id, intro_promo_state="past_intro")
    assert is_eligible_for_intro_promo(user_id, session) is False


def test_is_eligible_for_intro_promo_true_when_only_pre_first_payment(
    session: Session,
) -> None:
    """A subscription that never charged the first payment did not consume the promo."""
    user_id = _new_user_id()
    _make_subscription(session, user_id=user_id, intro_promo_state="pre_first_payment")
    assert is_eligible_for_intro_promo(user_id, session) is True


def test_is_eligible_for_intro_promo_true_when_only_skipped(session: Session) -> None:
    """A skipped intro (e.g. advocate tier where promo never applies) stays eligible."""
    user_id = _new_user_id()
    _make_subscription(session, user_id=user_id, intro_promo_state="skipped")
    assert is_eligible_for_intro_promo(user_id, session) is True


def test_is_eligible_for_intro_promo_false_mixed_states(session: Session) -> None:
    """If ANY subscription consumed the promo, lifetime-once denies the new one."""
    user_id = _new_user_id()
    _make_subscription(session, user_id=user_id, intro_promo_state="skipped")
    _make_subscription(session, user_id=user_id, intro_promo_state="past_intro")
    assert is_eligible_for_intro_promo(user_id, session) is False


def test_is_eligible_for_intro_promo_is_per_user(session: Session) -> None:
    """One user consuming the promo does not affect another user's eligibility."""
    consumer = _new_user_id()
    fresh = _new_user_id()
    _make_subscription(session, user_id=consumer, intro_promo_state="in_intro")
    assert is_eligible_for_intro_promo(consumer, session) is False
    assert is_eligible_for_intro_promo(fresh, session) is True
