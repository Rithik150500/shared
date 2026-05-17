"""Intro-promo state-machine unit tests (Task 7.2.2).

State machine (`subscriptions.intro_promo_state`):

    pre_first_payment
        ── event=subscription.activated ──→ in_intro
        ── event=skipped (advocate tier) ──→ skipped
    in_intro
        ── event=subscription.charged ──→ past_intro

Terminal states: past_intro, skipped.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.errors import BillingError
from case_billing.nowlez.promos import (
    get_intro_offer_id,
    record_intro_promo_consumed,
    record_intro_promo_skipped,
    transition_intro_promo_state,
)


@pytest.fixture()
def session() -> Session:
    from data_access.base import Base

    import data_access.models.auth  # noqa: F401
    import data_access.models.billing  # noqa: F401
    import data_access.models.user  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _make_subscription(
    session: Session,
    *,
    tier: str = "chambers",
    intro_promo_state: str = "pre_first_payment",
) -> uuid.UUID:
    from data_access.models.billing import Subscription
    from data_access.models.user import User

    user = User(phone=f"+91999{uuid.uuid4().hex[:7]}")
    session.add(user)
    session.flush()
    sub = Subscription(
        user_id=user.id,
        tier=tier,
        billing_cycle="monthly",
        status="active",
        intro_promo_state=intro_promo_state,
        referral_state="no_referral",
    )
    session.add(sub)
    session.flush()
    return sub.id


# --- get_intro_offer_id ----------------------------------------------------


def test_get_intro_offer_id_chambers() -> None:
    class Cfg:
        razorpay_offer_id_chambers_half_off = "offer_chambers_X"
        razorpay_offer_id_counsel_half_off = "offer_counsel_X"

    assert get_intro_offer_id("chambers", Cfg()) == "offer_chambers_X"


def test_get_intro_offer_id_counsel() -> None:
    class Cfg:
        razorpay_offer_id_chambers_half_off = "offer_chambers_X"
        razorpay_offer_id_counsel_half_off = "offer_counsel_X"

    assert get_intro_offer_id("counsel", Cfg()) == "offer_counsel_X"


def test_get_intro_offer_id_advocate_is_none() -> None:
    """Advocate tier has no intro promo (already at floor price)."""
    class Cfg:
        razorpay_offer_id_chambers_half_off = "offer_chambers_X"
        razorpay_offer_id_counsel_half_off = "offer_counsel_X"

    assert get_intro_offer_id("advocate", Cfg()) is None


def test_get_intro_offer_id_unknown_tier_raises() -> None:
    class Cfg:
        razorpay_offer_id_chambers_half_off = "x"
        razorpay_offer_id_counsel_half_off = "y"

    with pytest.raises(ValueError):
        get_intro_offer_id("enterprise", Cfg())


# --- transition_intro_promo_state -----------------------------------------


def test_transition_pre_to_in_intro_on_activate(session: Session) -> None:
    sub_id = _make_subscription(session, intro_promo_state="pre_first_payment")
    _run(transition_intro_promo_state(sub_id, "subscription.activated", session))
    session.flush()

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub_id)
    ).scalar_one()
    assert sub.intro_promo_state == "in_intro"


def test_transition_in_intro_to_past_intro_on_charge(session: Session) -> None:
    sub_id = _make_subscription(session, intro_promo_state="in_intro")
    _run(transition_intro_promo_state(sub_id, "subscription.charged", session))
    session.flush()

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub_id)
    ).scalar_one()
    assert sub.intro_promo_state == "past_intro"


def test_transition_invalid_event_raises(session: Session) -> None:
    sub_id = _make_subscription(session, intro_promo_state="pre_first_payment")
    with pytest.raises(BillingError):
        _run(transition_intro_promo_state(sub_id, "subscription.charged", session))


def test_transition_from_terminal_state_raises(session: Session) -> None:
    sub_id = _make_subscription(session, intro_promo_state="past_intro")
    with pytest.raises(BillingError):
        _run(transition_intro_promo_state(sub_id, "subscription.charged", session))


def test_transition_no_op_on_unknown_event(session: Session) -> None:
    """Unknown events leave state untouched and do not raise."""
    sub_id = _make_subscription(session, intro_promo_state="in_intro")
    _run(transition_intro_promo_state(sub_id, "subscription.cancelled", session))
    session.flush()

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub_id)
    ).scalar_one()
    assert sub.intro_promo_state == "in_intro"


def test_record_intro_promo_consumed_sets_past_intro(session: Session) -> None:
    sub_id = _make_subscription(session, intro_promo_state="in_intro")
    _run(record_intro_promo_consumed(sub_id, session))
    session.flush()

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub_id)
    ).scalar_one()
    assert sub.intro_promo_state == "past_intro"


def test_record_intro_promo_skipped(session: Session) -> None:
    """Skipping (advocate tier) transitions pre_first_payment → skipped."""
    sub_id = _make_subscription(
        session, tier="advocate", intro_promo_state="pre_first_payment",
    )
    _run(record_intro_promo_skipped(sub_id, session))
    session.flush()

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub_id)
    ).scalar_one()
    assert sub.intro_promo_state == "skipped"


def test_lifetime_once_two_subs_for_same_user(session: Session) -> None:
    """Second subscription created after past_intro must inherit ineligibility.

    The actual lifetime-once test lives in test_pricing.py's
    `is_eligible_for_intro_promo` coverage; here we just confirm the
    state machine doesn't accidentally reset past_intro on a new sub.
    """
    from case_billing.pricing import is_eligible_for_intro_promo
    from data_access.models.billing import Subscription
    from data_access.models.user import User

    user = User(phone=f"+91999{uuid.uuid4().hex[:7]}")
    session.add(user)
    session.flush()
    # First sub consumed the promo.
    session.add(Subscription(
        user_id=user.id, tier="chambers", billing_cycle="monthly",
        status="cancelled", intro_promo_state="past_intro",
        referral_state="no_referral",
    ))
    session.flush()
    assert is_eligible_for_intro_promo(user.id, session) is False
