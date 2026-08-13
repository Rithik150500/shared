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
    """ONE sellable plan as of 2026-08-10: chambers at ₹1,000, in paise.

    advocate/counsel were retired and are deliberately absent — this is a
    dict-equality check (not per-key lookups) so a retired key silently
    lingering in the mapping would fail this test too.
    """
    assert TIER_PRICES_PAISE == {"chambers": 100_000}  # ₹1,000


def test_referrer_mutual_paise_matches_spec() -> None:
    """Referral mutual-benefit survives the 2026-08-10 repricing: half a month."""
    assert REFERRER_MUTUAL_PAISE == {"chambers": 50_000}  # half of ₹1,000


# --- 5.1.1 calculate_first_payment_paise (cycle 1) --------------------------


@pytest.mark.parametrize(
    "tier,expected_paise",
    [
        ("chambers", 100_000),  # list price — the half-price intro promo
                                 # was retired 2026-08-10, see pricing.py docstring
    ],
)
def test_calculate_first_payment_paise(tier: str, expected_paise: int) -> None:
    assert calculate_first_payment_paise(tier) == expected_paise


@pytest.mark.parametrize("tier", ["counsel", "advocate"])
def test_calculate_first_payment_paise_rejects_retired_tiers(tier: str) -> None:
    """Counsel and advocate are retired from sale — nothing to charge."""
    with pytest.raises(ValueError):
        calculate_first_payment_paise(tier)


def test_calculate_first_payment_paise_rejects_unknown_tier() -> None:
    with pytest.raises(ValueError, match="Unknown or retired tier"):
        calculate_first_payment_paise("enterprise")


# --- 5.1.1 calculate_renewal_price_paise (cycle 2+) -------------------------


@pytest.mark.parametrize(
    "tier,cycle,has_referral,expected",
    [
        # Chambers — the only sellable tier as of the 2026-08-10 repricing.
        ("chambers", 2, False, 100_000),
        ("chambers", 2, True, 50_000),
        ("chambers", 3, False, 100_000),
        ("chambers", 3, True, 100_000),
        ("chambers", 6, False, 100_000),
    ],
)
def test_calculate_renewal_price_paise(
    tier: str, cycle: int, has_referral: bool, expected: int
) -> None:
    assert calculate_renewal_price_paise(tier, cycle, has_referral) == expected


@pytest.mark.parametrize("tier", ["counsel", "advocate"])
def test_calculate_renewal_price_paise_rejects_retired_tiers(tier: str) -> None:
    """Loud over silent: a retired tier is absent from TIER_PRICES_PAISE, so
    the lookup raises KeyError rather than billing an invented amount."""
    with pytest.raises(KeyError):
        calculate_renewal_price_paise(tier, cycle_number=2, has_pending_referral_mutual=False)


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


# --- Single-plan repricing (2026-08-10) --------------------------------------
#
# Chambers is now the only sellable tier; advocate/counsel are retired and
# deliberately absent from TIER_PRICES_PAISE. The half-price intro promo on
# cycle 1 was dropped by owner decision — calculate_first_payment_paise now
# always returns list price. The referral programme survives unchanged in
# shape, just repriced to half of the new ₹1,000.


def test_chambers_is_the_only_priced_tier():
    assert TIER_PRICES_PAISE == {"chambers": 100_000}


def test_first_payment_is_full_price_intro_promo_was_dropped():
    """The half-price first paid month was retired on 2026-08-10."""
    assert calculate_first_payment_paise("chambers") == 100_000


def test_first_payment_rejects_unknown_tiers():
    with pytest.raises(ValueError):
        calculate_first_payment_paise("counsel")


def test_referral_mutual_is_half_a_month():
    assert REFERRER_MUTUAL_PAISE["chambers"] == 50_000
    assert calculate_renewal_price_paise("chambers", 2, True) == 50_000
    assert calculate_renewal_price_paise("chambers", 3, True) == 100_000
    assert calculate_renewal_price_paise("chambers", 2, False) == 100_000


def test_retired_tier_renewal_raises_rather_than_inventing_a_price():
    """Loud over silent: a KeyError beats guessing what to bill."""
    with pytest.raises(KeyError):
        calculate_renewal_price_paise("counsel", 2, False)


def test_munshi_has_no_separate_per_case_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Munshi is bundled into the plan — there is nothing to invoice per case.

    BillingConfig has required (non-defaulted) Razorpay fields — see
    test_imports.py's REQUIRED_ENV — so they're stubbed here the same way,
    isolated from any developer .env via monkeypatch.chdir, to exercise the
    default rather than a real deployment's env.
    """
    monkeypatch.chdir(__import__("tempfile").mkdtemp())
    for key, value in {
        "RAZORPAY_KEY_ID": "rzp_test_key",
        "RAZORPAY_KEY_SECRET": "rzp_test_secret",
        "RAZORPAY_WEBHOOK_SECRET": "rzp_webhook_secret",
        "RAZORPAY_PLAN_ID_ADVOCATE_MONTHLY": "plan_advocate_m",
        "RAZORPAY_PLAN_ID_COUNSEL_MONTHLY": "plan_counsel_m",
        "RAZORPAY_PLAN_ID_CHAMBERS_MONTHLY": "plan_chambers_m",
        "RAZORPAY_PLAN_ID_CHAMBERS_QUARTERLY": "plan_chambers_q",
        "RAZORPAY_PLAN_ID_CHAMBERS_YEARLY": "plan_chambers_y",
    }.items():
        monkeypatch.setenv(key, value)

    from case_billing.config import BillingConfig

    assert BillingConfig().munshi_price_per_case_paise == 0
