"""Tier-selection + subscription-creation integration tests (Task 7.4).

`select_tier_and_subscribe(user_id, chosen_tier, referral_code, session,
razorpay_client, config)` orchestrates:

  verify users_nowlez.tier IS NULL (else TierAlreadySelected)
  → verify chosen_tier in ('advocate','counsel','chambers')
  → check intro promo eligibility (lifetime-once)
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
    assert sub.intro_promo_state == "pre_first_payment"
    assert sub.referral_state == "no_referral"

    nowlez = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one()
    assert nowlez.tier == "chambers"

    # Razorpay was called with plan + intro offer (chambers eligible).
    assert len(client.calls) == 1
    assert client.calls[0]["plan_id"] == "plan_cham_m"
    assert client.calls[0]["offer_id"] == "offer_cham_half"


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
