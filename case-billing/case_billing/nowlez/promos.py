"""Intro-promo state machine for Nowlez subscriptions (Task 7.2).

Spec Section 1.4 Q2: cycle-1 ("first paid month after the trial") on
Chambers and Counsel ships at half price via a lifetime-once Razorpay
offer. Advocate is *excluded* because it's already at the floor price.

State machine on ``subscriptions.intro_promo_state``:

    pre_first_payment ── subscription.activated  ──→  in_intro
    pre_first_payment ── (advocate: skipped)     ──→  skipped     (terminal)
    in_intro          ── subscription.charged    ──→  past_intro  (terminal)

Terminal states (``past_intro`` and ``skipped``) cannot transition; the
lifetime-once eligibility check in :func:`case_billing.pricing.is_eligible_for_intro_promo`
treats them differently (past_intro consumes the promo; skipped does
not, so e.g. an advocate-then-counsel sequence can still claim half
off on the second sub).

Public surface:

* :func:`get_intro_offer_id` — pure mapping from tier+config → offer id.
* :func:`transition_intro_promo_state` — async; the state-machine entry
  point called from webhook handlers.
* :func:`record_intro_promo_consumed` — alias for the in_intro→past_intro
  transition (terminal); used by the subscription.charged handler.
* :func:`record_intro_promo_skipped` — pre→skipped (terminal) used when
  the user picks the advocate tier.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from case_billing.errors import BillingError
# Sub-project C Phase 1: re-export the lifetime-once eligibility check
# from case_billing.pricing under the nowlez.promos namespace so callers
# can import it alongside the rest of the intro-promo surface. The
# canonical implementation lives in pricing.py because the lifetime-once
# rule sits at the price-decision boundary (calculate_first_payment_paise
# also reads the same column), not in the state machine here.
from case_billing.pricing import is_eligible_for_intro_promo  # noqa: F401


# State machine — keys are (from_state, event); values are the target state.
_TRANSITIONS: dict[tuple[str, str], str] = {
    ("pre_first_payment", "subscription.activated"): "in_intro",
    ("in_intro", "subscription.charged"): "past_intro",
}

# Events the state machine recognises but that don't fire a transition
# from any state (logged-and-ignored). Anything outside both maps is
# a soft no-op so the webhook router can blanket-forward events.
_INERT_EVENTS: frozenset[str] = frozenset({
    "subscription.cancelled",
    "subscription.completed",
    "subscription.halted",
})

_TERMINAL_STATES: frozenset[str] = frozenset({"past_intro", "skipped"})


def get_intro_offer_id(tier: str, config: Any) -> str | None:
    """Return the Razorpay offer-id for this tier's half-price intro promo.

    Args:
        tier: Subscription tier name.
        config: A ``BillingConfig`` (or any duck-typed object exposing
            ``razorpay_offer_id_chambers_half_off`` and
            ``razorpay_offer_id_counsel_half_off``).

    Returns:
        ``None`` for advocate (no promo); the offer-id string for
        chambers and counsel.

    Raises:
        ValueError: For unknown tier names.
    """
    if tier == "chambers":
        return config.razorpay_offer_id_chambers_half_off
    if tier == "counsel":
        return config.razorpay_offer_id_counsel_half_off
    if tier == "advocate":
        return None
    raise ValueError(f"Unknown tier: {tier!r}")


async def transition_intro_promo_state(
    subscription_id: uuid.UUID,
    event: str,
    session: Session,
) -> str:
    """Move a subscription's intro_promo_state in response to a webhook event.

    Args:
        subscription_id: Primary key of the ``subscriptions`` row.
        event: Razorpay webhook event type (e.g. ``"subscription.activated"``).
        session: Open SQLAlchemy session.

    Returns:
        The state name after the transition (== the pre-state when the
        event is inert).

    Raises:
        :class:`case_billing.errors.BillingError` when:
            * The subscription is missing.
            * The current state is terminal (past_intro / skipped).
            * The event is "active" (would otherwise transition) but
              illegal from the current state.
    """
    from data_access.models.billing import Subscription

    sub = session.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    ).scalar_one_or_none()
    if sub is None:
        raise BillingError(
            f"transition_intro_promo_state: subscription {subscription_id} not found"
        )

    # Inert events are accepted everywhere — no-op.
    if event in _INERT_EVENTS:
        return sub.intro_promo_state

    # Terminal states cannot transition.
    if sub.intro_promo_state in _TERMINAL_STATES:
        raise BillingError(
            f"transition_intro_promo_state: subscription {subscription_id} is "
            f"in terminal state {sub.intro_promo_state!r}; cannot apply {event!r}"
        )

    target = _TRANSITIONS.get((sub.intro_promo_state, event))
    if target is None:
        # Event is recognised by name but illegal from this state.
        raise BillingError(
            f"transition_intro_promo_state: illegal transition "
            f"({sub.intro_promo_state!r}, {event!r})"
        )
    sub.intro_promo_state = target
    session.flush()
    return target


async def record_intro_promo_consumed(
    subscription_id: uuid.UUID, session: Session,
) -> None:
    """Mark the intro promo as consumed (``past_intro``).

    Convenience for the webhook handler that knows it's processing a
    successful charge — equivalent to passing event=subscription.charged
    to :func:`transition_intro_promo_state`. No-op if already terminal.
    """
    from data_access.models.billing import Subscription

    sub = session.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    ).scalar_one_or_none()
    if sub is None:
        raise BillingError(
            f"record_intro_promo_consumed: subscription {subscription_id} not found"
        )
    if sub.intro_promo_state in _TERMINAL_STATES:
        return
    sub.intro_promo_state = "past_intro"
    session.flush()


async def record_intro_promo_skipped(
    subscription_id: uuid.UUID, session: Session,
) -> None:
    """Mark the intro promo as skipped (``skipped``).

    Called when the user picks the Advocate tier at signup, since
    Advocate is excluded from the intro promo entirely. ``skipped`` is
    a terminal state that does NOT consume the lifetime-once
    eligibility — see :func:`case_billing.pricing.is_eligible_for_intro_promo`.
    """
    from data_access.models.billing import Subscription

    sub = session.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    ).scalar_one_or_none()
    if sub is None:
        raise BillingError(
            f"record_intro_promo_skipped: subscription {subscription_id} not found"
        )
    if sub.intro_promo_state in _TERMINAL_STATES:
        return
    sub.intro_promo_state = "skipped"
    session.flush()
