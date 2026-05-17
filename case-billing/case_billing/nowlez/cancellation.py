"""Nowlez subscription cancellation + downgrade (Task 7.6).

Two entry points:

* :func:`cancel_subscription` — user-driven "cancel my plan" path:
  cancels the upstream Razorpay subscription with
  ``cancel_at_cycle_end=True`` (so the user keeps service through the
  end of the period they already paid for), flips local status to
  ``cancelled``, stamps ``cancel_at``, sends the
  ``nowlez_subscription_cancelled_v1`` template.
* :func:`downgrade_subscription` — schedule a tier change at the next
  cycle boundary. The current cycle is preserved (user paid for it);
  the new tier kicks in when the cycle ends. Implementation: cancel
  the current subscription at cycle end, leave a TODO row that the
  next-cycle scheduler picks up. We audit the intent so admin tooling
  can see it.

Both functions take ``razorpay_client`` so the caller can supply any
configured Razorpay HTTP client; the Razorpay cancel function is
imported under a private alias so unit tests can monkeypatch it.

The subscription status moves to ``cancelled`` directly rather than to
a ``pending_cancellation`` interim because the existing
``subscriptions_status_check`` constraint doesn't permit the latter
(spec wanted it, but the migration's enum is narrower). If a future
migration adds the interim state, swap the literal in one place here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from case_billing.errors import SubscriptionNotActive
from case_billing.razorpay_client.subscriptions import (
    cancel_subscription as rzp_cancel_subscription,
)


_VALID_TIERS: frozenset[str] = frozenset({"advocate", "counsel", "chambers"})


async def cancel_subscription(
    *,
    user_id: uuid.UUID,
    session: Session,
    razorpay_client: Any,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> None:
    """Cancel the user's active Nowlez subscription at cycle end.

    Raises:
        :class:`case_billing.errors.SubscriptionNotActive`: when the
            user has no subscription in a cancellable status
            (``trialing`` or ``active``).
    """
    from data_access.models.audit import AuditLog
    from data_access.models.billing import Subscription
    from data_access.models.user import User

    sub = session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .where(Subscription.status.in_(("trialing", "active", "past_due")))
        .order_by(Subscription.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if sub is None:
        raise SubscriptionNotActive(
            f"No active subscription for user {user_id}"
        )

    if sub.razorpay_subscription_id:
        await rzp_cancel_subscription(
            razorpay_client,
            sub.razorpay_subscription_id,
            cancel_at_cycle_end=True,
        )

    sub.status = "cancelled"
    sub.cancel_at = sub.period_end or datetime.now(timezone.utc)
    session.flush()

    phone = session.execute(
        select(User.phone).where(User.id == user_id)
    ).scalar_one_or_none()
    if phone:
        await send_template_fn(
            to=phone,
            template="nowlez_subscription_cancelled_v1",
            variables={"cancel_at": sub.cancel_at.isoformat() if sub.cancel_at else ""},
            brand="nowlez",
        )

    session.add(AuditLog(
        event_type="nowlez.subscription_cancelled",
        user_id=user_id,
        source="nowlez",
        metadata_={"subscription_id": str(sub.id)},
    ))
    session.flush()


async def downgrade_subscription(
    *,
    user_id: uuid.UUID,
    new_tier: str,
    session: Session,
    razorpay_client: Any,
) -> None:
    """Schedule a tier downgrade to take effect at the next cycle boundary.

    Implementation: cancel the current Razorpay subscription with
    ``cancel_at_cycle_end=True`` and write an audit row capturing the
    intended new tier. A separate scheduler / webhook handler picks up
    that intent at the cycle end and re-runs :func:`select_tier_and_subscribe`
    with the new tier.

    Raises:
        :class:`ValueError`: when ``new_tier`` is not a recognised tier.
        :class:`case_billing.errors.SubscriptionNotActive`: when the
            user has no subscription in a cancellable status.
    """
    if new_tier not in _VALID_TIERS:
        raise ValueError(f"Unknown tier: {new_tier!r}")

    from data_access.models.audit import AuditLog
    from data_access.models.billing import Subscription

    sub = session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .where(Subscription.status.in_(("trialing", "active", "past_due")))
        .order_by(Subscription.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if sub is None:
        raise SubscriptionNotActive(
            f"No active subscription for user {user_id} to downgrade"
        )

    if sub.razorpay_subscription_id:
        await rzp_cancel_subscription(
            razorpay_client,
            sub.razorpay_subscription_id,
            cancel_at_cycle_end=True,
        )

    sub.cancel_at = sub.period_end or datetime.now(timezone.utc)
    # Status stays trialing/active until the scheduler picks up the
    # downgrade intent at cycle end; the audit row is the source of
    # truth for "downgrade pending".
    session.add(AuditLog(
        event_type="nowlez.subscription_downgrade_scheduled",
        user_id=user_id,
        source="nowlez",
        metadata_={
            "subscription_id": str(sub.id),
            "current_tier": sub.tier,
            "new_tier": new_tier,
            "effective_at": (
                sub.cancel_at.isoformat() if sub.cancel_at else ""
            ),
        },
    ))
    session.flush()
