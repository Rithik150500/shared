"""Tier-selection + subscription-creation integration tests (Task 7.4).

`select_tier_and_subscribe(user_id, chosen_tier, referral_code, session,
razorpay_client, config)` orchestrates:

  verify users_nowlez.tier IS NULL (else TierAlreadySelected)
  → verify chosen_tier in ('advocate','counsel','chambers')
  → intro-promo offer attachment (RETIRED 2026-08-10 — always omitted now)
  → find or create referral
  → call Razorpay subscriptions.create_subscription with plan_id + offer
  → INSERT subscriptions row
  → UPDATE users_nowlez.tier = chosen_tier
  → audit log
  → return Razorpay short_url
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.errors import TierAlreadySelected
from case_billing.nowlez.subscriptions import select_tier_and_subscribe


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


def _make_trial_user(
    session: Session,
    *,
    referral_code: str | None = None,
) -> uuid.UUID:
    from data_access.models.user import User, UserNowlez

    user = User(phone=f"+91999{uuid.uuid4().hex[:7]}")
    session.add(user)
    session.flush()
    session.add(
        UserNowlez(
            user_id=user.id, name="Trial User",
            tier=None,
            trial_started_at=datetime.now(timezone.utc),
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=15),
            referral_code=referral_code or uuid.uuid4().hex[:8],
        )
    )
    session.flush()
    return user.id


class _Cfg:
    razorpay_plan_id_advocate_monthly = "plan_adv_m"
    razorpay_plan_id_counsel_monthly = "plan_cou_m"
    razorpay_plan_id_chambers_monthly = "plan_cham_m"
    razorpay_plan_id_chambers_quarterly = "plan_cham_q"
    razorpay_plan_id_chambers_yearly = "plan_cham_y"
    razorpay_offer_id_chambers_half_off = "offer_cham_half"
    razorpay_offer_id_counsel_half_off = "offer_cou_half"


async def _fake_create_subscription(client, **kwargs):
    client.calls.append(kwargs)
    from case_billing.razorpay_client.subscriptions import RazorpaySubscription
    return RazorpaySubscription(
        id="sub_test_abc",
        short_url="https://rzp.io/i/sub_test",
        status="created",
        current_start=None,
        current_end=None,
        notes=kwargs.get("notes", {}),
    )


class FakeRazorpayClient:
    def __init__(self):
        self.calls = []


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    from case_billing.nowlez import subscriptions as subs_mod

    monkeypatch.setattr(
        subs_mod, "rzp_create_subscription", _fake_create_subscription,
    )


# --- happy path ------------------------------------------------------------


def test_select_chambers_creates_subscription_and_updates_tier(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    user_id = _make_trial_user(session)
    client = FakeRazorpayClient()

    short_url = _run(
        select_tier_and_subscribe(
            user_id=user_id,
            chosen_tier="chambers",
            referral_code=None,
            session=session,
            razorpay_client=client,
            config=_Cfg(),
        )
    )
    session.flush()

    assert short_url == "https://rzp.io/i/sub_test"

    from data_access.models.billing import Subscription
    from data_access.models.user import UserNowlez

    sub = session.execute(
        select(Subscription).where(Subscription.user_id == user_id)
    ).scalar_one()
    assert sub.tier == "chambers"
    assert sub.billing_cycle == "monthly"
    assert sub.razorpay_subscription_id == "sub_test_abc"
    # The intro promo was retired 2026-08-10 — no offer is ever attached, so
    # the ledger starts (and stays, being terminal) at the non-consuming
    # 'skipped' state rather than 'pre_first_payment', which would otherwise
    # transition to the CONSUMING 'in_intro' on subscription.activated even
    # though this subscriber was never actually given a discount. See
    # test_chambers_never_attaches_intro_offer_even_when_configured below
    # for the guard that suppression holds even with a real offer id set.
    assert sub.intro_promo_state == "skipped"
    assert sub.referral_state == "no_referral"

    nowlez = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one()
    assert nowlez.tier == "chambers"

    # Razorpay was called with the plan only — no intro offer (retired 2026-08-10).
    assert len(client.calls) == 1
    assert client.calls[0]["plan_id"] == "plan_cham_m"
    assert client.calls[0].get("offer_id") is None


def test_chambers_never_attaches_intro_offer_even_when_configured(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the 2026-08-10 intro-promo retirement.

    _Cfg.razorpay_offer_id_chambers_half_off is deliberately a real
    (non-None) value here — the same config used by the happy-path test
    above — to prove suppression lives in select_tier_and_subscribe's code
    path and isn't merely an artifact of an unset env var in production.

    If this test ever starts failing because an offer_id is attached again,
    case_billing.pricing.calculate_first_payment_paise and the tier picker
    UI must both change to advertise a discount first — otherwise the card
    promises list price while Razorpay bills half of it, which is a
    chargeback, not a discount.
    """
    _patch(monkeypatch)
    user_id = _make_trial_user(session)
    client = FakeRazorpayClient()
    config = _Cfg()
    assert config.razorpay_offer_id_chambers_half_off is not None  # sanity: the guard is real

    _run(
        select_tier_and_subscribe(
            user_id=user_id, chosen_tier="chambers", referral_code=None,
            session=session, razorpay_client=client, config=config,
        )
    )
    session.flush()

    assert client.calls[0].get("offer_id") is None

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.user_id == user_id)
    ).scalar_one()
    assert sub.intro_promo_state == "skipped"


def test_select_advocate_omits_intro_offer(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    user_id = _make_trial_user(session)
    client = FakeRazorpayClient()

    _run(
        select_tier_and_subscribe(
            user_id=user_id, chosen_tier="advocate", referral_code=None,
            session=session, razorpay_client=client, config=_Cfg(),
        )
    )
    session.flush()

    assert client.calls[0]["plan_id"] == "plan_adv_m"
    # Advocate gets no intro promo.
    assert client.calls[0].get("offer_id") is None


def test_already_selected_tier_raises(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    user_id = _make_trial_user(session)
    client = FakeRazorpayClient()
    _run(
        select_tier_and_subscribe(
            user_id=user_id, chosen_tier="counsel", referral_code=None,
            session=session, razorpay_client=client, config=_Cfg(),
        )
    )
    session.flush()

    with pytest.raises(TierAlreadySelected):
        _run(
            select_tier_and_subscribe(
                user_id=user_id, chosen_tier="chambers", referral_code=None,
                session=session, razorpay_client=client, config=_Cfg(),
            )
        )


def test_unknown_tier_raises_value_error(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    user_id = _make_trial_user(session)
    client = FakeRazorpayClient()
    with pytest.raises(ValueError):
        _run(
            select_tier_and_subscribe(
                user_id=user_id, chosen_tier="enterprise", referral_code=None,
                session=session, razorpay_client=client, config=_Cfg(),
            )
        )


def test_select_with_referral_sets_pending_mutual(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    # Referrer with code.
    from data_access.models.user import User, UserNowlez

    referrer = User(phone="+919998880001")
    session.add(referrer)
    session.flush()
    session.add(UserNowlez(
        user_id=referrer.id, name="R", tier="chambers",
        referral_code="REFGOOD",
    ))
    session.flush()

    referred = _make_trial_user(session)
    client = FakeRazorpayClient()

    _run(
        select_tier_and_subscribe(
            user_id=referred, chosen_tier="counsel",
            referral_code="REFGOOD",
            session=session, razorpay_client=client, config=_Cfg(),
        )
    )
    session.flush()

    from data_access.models.billing import Referral, Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.user_id == referred)
    ).scalar_one()
    assert sub.referral_state == "pending_mutual"

    referrals = session.execute(
        select(Referral).where(Referral.referred_user_id == referred)
    ).scalars().all()
    assert len(referrals) == 1
