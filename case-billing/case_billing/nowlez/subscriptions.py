"""Nowlez tier selection + subscription lifecycle (Task 7.4 + 7.6).

Top-level orchestrator :func:`select_tier_and_subscribe` is called by
the "pick tier" route at the end of the trial:

  verify users_nowlez.tier IS NULL → raise TierAlreadySelected if not
  → verify chosen_tier ∈ {'advocate','counsel','chambers'}
  → check intro-promo eligibility (lifetime-once)
  → find_or_create_referral if a code was supplied
  → call Razorpay subscriptions.create_subscription with plan + offer
  → INSERT subscriptions row (status='created')
  → UPDATE users_nowlez.tier = chosen_tier
  → write audit log 'nowlez.tier_selected'
  → return Razorpay short_url

Three lighter-weight helpers round out the module:

* :func:`select_tier` — pure validation alias for routes that want to
  short-circuit before any Razorpay call.
* :func:`activate_subscription` — webhook handler for
  `subscription.activated` (status='active').
* :func:`mark_past_due` / :func:`cancel_subscription` are imported
  from `cancellation.py` and re-exported here so consumers don't need
  to know which submodule owns the verb.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from case_billing.errors import TierAlreadySelected
from case_billing.nowlez.promos import get_intro_offer_id
from case_billing.nowlez.referrals import find_or_create_referral
from case_billing.pricing import is_eligible_for_intro_promo
from case_billing.razorpay_client.subscriptions import (
    create_subscription as rzp_create_subscription,
)


# Tiers we recognise. The set is duplicated in the SQL CHECK constraint
# on `subscriptions.tier`; mirroring it here lets us fail loudly with a
# Python ValueError before any Razorpay round-trip happens.
_VALID_TIERS: frozenset[str] = frozenset({"advocate", "counsel", "chambers"})


def select_tier(tier: str) -> str:
    """Validate a tier name; return it verbatim or raise.

    Cheap helper so routes can short-circuit before they reach the
    Razorpay call site.
    """
    if tier not in _VALID_TIERS:
        raise ValueError(f"Unknown tier: {tier!r}")
    return tier


async def select_tier_and_subscribe(
    *,
    user_id: uuid.UUID,
    chosen_tier: str,
    referral_code: str | None,
    session: Session,
    razorpay_client: Any,
    config: Any,
) -> str:
    """End-of-trial pick-a-tier orchestrator. Returns the Razorpay short_url.

    Args:
        user_id: The trial user who is choosing a tier.
        chosen_tier: One of 'advocate', 'counsel', 'chambers'.
        referral_code: Optional referrer code (None when no referral).
        session: Open SQLAlchemy session.
        razorpay_client: A configured RazorpayHTTPClient.
        config: A BillingConfig (or compatible) — supplies plan and
            offer ids.

    Raises:
        :class:`case_billing.errors.TierAlreadySelected`: when the
            user already has a tier set on ``users_nowlez``.
        :class:`ValueError`: when ``chosen_tier`` is not recognised.
    """
    from data_access.models.audit import AuditLog
    from data_access.models.billing import Subscription
    from data_access.models.user import UserNowlez

    select_tier(chosen_tier)

    # 1. Tier guard.
    nowlez_row = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one_or_none()
    if nowlez_row is None:
        # Should not normally happen — trial signup creates the row.
        # We let it through anyway by creating a thin row below.
        nowlez_row = UserNowlez(user_id=user_id, name="")
        session.add(nowlez_row)
        session.flush()
    if nowlez_row.tier is not None:
        raise TierAlreadySelected(
            f"User {user_id} already has tier {nowlez_row.tier!r}"
        )

    # 2. Intro promo eligibility + offer id.
    intro_eligible = is_eligible_for_intro_promo(user_id, session)
    intro_offer_id = (
        get_intro_offer_id(chosen_tier, config) if intro_eligible else None
    )

    # 3. Referral attachment (no-op for None code).
    referral = None
    if referral_code:
        referral = await find_or_create_referral(
            referred_user_id=user_id,
            referral_code=referral_code,
            session=session,
        )

    # 4. Razorpay subscription create.
    plan_id = _plan_id_for(chosen_tier, config)
    notes = {
        "product": "nowlez",
        "user_id": str(user_id),
        "tier": chosen_tier,
    }
    if referral is not None:
        notes["referral_id"] = str(referral.id)

    rzp_subscription = await rzp_create_subscription(
        razorpay_client,
        plan_id=plan_id,
        customer_notify=True,
        total_count=12,  # 12 monthly cycles before auto-renewal renewal
        notes=notes,
        offer_id=intro_offer_id,
    )

    # 5. Local subscription row. Use 'trialing' to satisfy the
    # subscriptions_status_check constraint (no 'created' allowed there);
    # the webhook handler flips to 'active' on subscription.activated.
    sub = Subscription(
        user_id=user_id,
        tier=chosen_tier,
        billing_cycle="monthly",
        razorpay_subscription_id=rzp_subscription.id,
        status="trialing",
        intro_promo_state="pre_first_payment",
        referral_state="pending_mutual" if referral is not None else "no_referral",
    )
    session.add(sub)
    session.flush()

    # 6. Mark the tier picked on the extension.
    nowlez_row.tier = chosen_tier

    # 7. Audit log.
    session.add(AuditLog(
        event_type="nowlez.tier_selected",
        user_id=user_id,
        source="nowlez",
        metadata_={
            "tier": chosen_tier,
            "intro_promo": intro_offer_id is not None,
            "referral_id": (str(referral.id) if referral is not None else None),
        },
    ))
    session.flush()

    return rzp_subscription.short_url


def _plan_id_for(tier: str, config: Any) -> str:
    """Return the monthly Razorpay plan_id for the given tier."""
    if tier == "chambers":
        return config.razorpay_plan_id_chambers_monthly
    if tier == "counsel":
        return config.razorpay_plan_id_counsel_monthly
    if tier == "advocate":
        return config.razorpay_plan_id_advocate_monthly
    raise ValueError(f"Unknown tier: {tier!r}")


# --- webhook-driven state transitions -------------------------------------


async def activate_subscription(
    subscription_id: uuid.UUID,
    session: Session,
) -> None:
    """Flip ``status='active'`` on the local subscription row.

    Called from the ``subscription.activated`` webhook handler.
    Idempotent — re-running on an already-active subscription is a
    no-op.
    """
    from data_access.models.billing import Subscription

    sub = session.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    ).scalar_one_or_none()
    if sub is None or sub.status == "active":
        return
    sub.status = "active"
    session.flush()


async def mark_past_due(
    subscription_id: uuid.UUID,
    session: Session,
) -> None:
    """Flip ``status='past_due'`` (called from ``subscription.halted``)."""
    from data_access.models.billing import Subscription

    sub = session.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    ).scalar_one_or_none()
    if sub is None or sub.status == "past_due":
        return
    sub.status = "past_due"
    session.flush()


__all__ = [
    "select_tier",
    "select_tier_and_subscribe",
    "activate_subscription",
    "mark_past_due",
]
