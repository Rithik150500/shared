"""Cancellation + downgrade tests (Task 7.6.2)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.errors import SubscriptionNotActive
from case_billing.nowlez.cancellation import (
    cancel_subscription,
    downgrade_subscription,
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


def _make_user_with_active_sub(
    session: Session,
    *,
    tier: str = "chambers",
    status: str = "active",
) -> tuple[uuid.UUID, uuid.UUID]:
    from data_access.models.billing import Subscription
    from data_access.models.user import User, UserNowlez

    user = User(phone=f"+91999{uuid.uuid4().hex[:7]}")
    session.add(user)
    session.flush()
    session.add(UserNowlez(user_id=user.id, name="X", tier=tier))
    session.flush()
    sub = Subscription(
        user_id=user.id, tier=tier, billing_cycle="monthly",
        razorpay_subscription_id="sub_existing",
        status=status, intro_promo_state="past_intro",
        referral_state="no_referral",
        period_end=datetime.now(timezone.utc) + timedelta(days=15),
    )
    session.add(sub)
    session.flush()
    return user.id, sub.id


class FakeRazorpay:
    def __init__(self):
        self.cancel_calls = []


async def _fake_cancel(client, sub_id, cancel_at_cycle_end: bool = True):
    client.cancel_calls.append({
        "subscription_id": sub_id,
        "cancel_at_cycle_end": cancel_at_cycle_end,
    })
    from case_billing.razorpay_client.subscriptions import RazorpaySubscription
    return RazorpaySubscription(
        id=sub_id,
        short_url="",
        status="cancelled",
        current_start=None,
        current_end=None,
        notes={},
    )


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    from case_billing.nowlez import cancellation as cmod

    monkeypatch.setattr(cmod, "rzp_cancel_subscription", _fake_cancel)


# --- cancel_subscription --------------------------------------------------


def test_cancel_subscription_calls_razorpay_with_cycle_end(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    user_id, sub_id = _make_user_with_active_sub(session)
    sender = AsyncMock()
    client = FakeRazorpay()

    _run(cancel_subscription(
        user_id=user_id, session=session,
        razorpay_client=client, send_template_fn=sender,
    ))
    session.flush()

    assert len(client.cancel_calls) == 1
    assert client.cancel_calls[0]["cancel_at_cycle_end"] is True

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub_id)
    ).scalar_one()
    # Use cancelled since 'pending_cancellation' is not in the constraint
    # (subscriptions_status_check), see cancellation.py docstring.
    assert sub.status == "cancelled"
    assert sub.cancel_at is not None
    sender.assert_called_once()
    assert sender.call_args.kwargs["template"] == "nowlez_subscription_cancelled_v1"


def test_cancel_subscription_when_no_active_raises(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    from data_access.models.user import User

    user = User(phone="+919998888888")
    session.add(user)
    session.flush()

    with pytest.raises(SubscriptionNotActive):
        _run(cancel_subscription(
            user_id=user.id, session=session,
            razorpay_client=FakeRazorpay(), send_template_fn=AsyncMock(),
        ))


# --- downgrade_subscription -----------------------------------------------


def test_downgrade_schedules_new_tier_at_cycle_end(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    user_id, sub_id = _make_user_with_active_sub(session, tier="chambers")
    client = FakeRazorpay()

    _run(downgrade_subscription(
        user_id=user_id, new_tier="counsel",
        session=session, razorpay_client=client,
    ))
    session.flush()

    # Existing subscription cancelled at cycle end.
    assert len(client.cancel_calls) == 1
    assert client.cancel_calls[0]["cancel_at_cycle_end"] is True

    from data_access.models.audit import AuditLog
    log = session.execute(
        select(AuditLog)
        .where(AuditLog.event_type == "nowlez.subscription_downgrade_scheduled")
    ).scalar_one()
    assert log.metadata_["new_tier"] == "counsel"


def test_downgrade_unknown_tier_raises(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    user_id, _ = _make_user_with_active_sub(session)
    with pytest.raises(ValueError):
        _run(downgrade_subscription(
            user_id=user_id, new_tier="enterprise",
            session=session, razorpay_client=FakeRazorpay(),
        ))
