"""Day-31 fallback paths for trial-lapsed Nowlez users (Task 7.5 + 14).

Two policies, switchable per ``BillingConfig.nowlez_lapsed_trial_action``:

* :func:`fallback_to_munshi` (default) — graceful: create a Munshi
  extension for the user, open billing periods for their active cases,
  pause anything over 200 cases (oldest first), and notify. The user
  keeps service but starts paying Munshi-style postpaid.

* :func:`freeze_account` — hard stop: set every case to
  ``refresh_enabled=False``, notify. Used when the operator wants to
  withhold service until the user manually picks a tier.

The high-level entry point :func:`apply_lapsed_trial_action` reads the
``BillingConfig.nowlez_lapsed_trial_action`` knob and dispatches to one
of the two above. The day-31 cron should call *only* the high-level
entry point so the choice of policy is operator-tunable without code
edits.

Both paths leave ``users_nowlez.tier`` as ``NULL`` so the user can still
pick a tier later and be upgraded back to a subscription. They both
write an audit log entry so the cron is observable.

Task 14 edge cases handled inline by each entry point:

* **Zero saved cases** at day 31 → don't open Munshi billing; just
  audit + send the ``nowlez_trial_ended_no_billing_v1`` template. The
  user can re-engage later via re-engagement campaign.
* **User already has paid Nowlez tier at day 31** (raced selection) →
  no-op + audit. The trial state was overtaken; bailing out cleanly
  avoids creating spurious Munshi rows that would confuse the eligibility
  predicate.
* **User already has a Munshi extension** at day 31 (the Munshi-only
  signup flow ran first) → reuse the existing row, just open
  ``case_billing_periods`` for active cases.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import asc, select, update
from sqlalchemy.orm import Session

from case_billing.metrics import (
    billing_nowlez_trial_fallback_freeze_total,
    billing_nowlez_trial_fallback_munshi_total,
    billing_nowlez_trial_fallback_noop_total,
)


# Sub-project E spec: cap at 200 cases when falling back from Nowlez to
# Munshi (spec Section 6.4 — "User has 200+ cases when falling back").
NOWLEZ_FALLBACK_CASE_CAP: int = 200


# Tiers that count as "user already paying Nowlez" — copied from
# `case_billing.shared.eligibility` so we don't introduce a circular
# import (fallback can't depend on eligibility because the webhook
# router that uses eligibility also lazily imports fallback).
_PAID_NOWLEZ_TIERS: frozenset[str] = frozenset({"advocate", "counsel", "chambers"})


# Recognised values for `BillingConfig.nowlez_lapsed_trial_action`.
LAPSED_TRIAL_ACTION_FALLBACK: str = "fallback_to_munshi"
LAPSED_TRIAL_ACTION_FREEZE: str = "freeze_account"
_VALID_LAPSED_ACTIONS: frozenset[str] = frozenset({
    LAPSED_TRIAL_ACTION_FALLBACK,
    LAPSED_TRIAL_ACTION_FREEZE,
})


async def fallback_to_munshi(
    user_id: uuid.UUID,
    session: Session,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> None:
    """Convert a lapsed-trial Nowlez user into a Munshi postpaid user.

    Side-effects (in order), gated by the Task 14 edge cases:

    a. **Raced paid tier with active subscription** — if
       ``users_nowlez.tier`` is already one of (advocate/counsel/chambers)
       AND the user holds a subscription in a billable status (``trialing``
       / ``active`` / ``past_due``), they picked a tier after the cron
       picked them up; no-op + audit row and return early. We *don't*
       short-circuit when the only matching subscription is already
       cancelled or expired because that's the subscription.cancelled-→
       fallback path (the cancellation flow leaves ``users_nowlez.tier``
       set but the subscription is no longer billing).
    b. **No saved cases** — if the user has zero active cases at day
       31, opening a Munshi extension would charge ₹0 every cycle. We
       skip the billing setup entirely, send the
       ``nowlez_trial_ended_no_billing_v1`` template, audit, and bail.

    Otherwise:

    1. Create (or no-op if exists) a ``users_munshi`` row with
       ``billing_anniversary_date = today_ist``.
    2. For each active case (refresh_enabled=True), insert a fresh
       ``case_billing_periods`` row (period_end=NULL).
    3. If the user has > 200 active cases, pause the oldest down to
       200: flip ``refresh_enabled=False`` and close the just-opened
       billing period for those cases.
    4. Send the ``nowlez_trial_fallback_v1`` template (graceful, not a
       hard suspension — the user keeps access on the active cases).
    5. Audit log ``event_type='nowlez.trial_fallback_to_munshi'``.

    `users_nowlez.tier` is **not** touched — the user can still pick a
    Nowlez tier later and the upgrade flow will close the Munshi
    extension at that point.
    """
    from data_access.models.audit import AuditLog
    from data_access.models.billing import (
        CaseBillingPeriod, Subscription,
    )
    from data_access.models.case import Case
    from data_access.models.user import User, UserMunshi, UserNowlez

    now = datetime.now(timezone.utc)
    today = date.today()

    # Edge case (a): raced paid tier WITH an active subscription. If the
    # user picked a tier and that subscription is still in a billable
    # status, the fallback is the wrong action — bail and audit.
    # We deliberately *don't* short-circuit when tier is set but the
    # only matching subscription is cancelled/expired; that's the
    # post-cancellation hand-off path the webhook router relies on.
    nowlez_tier = session.execute(
        select(UserNowlez.tier).where(UserNowlez.user_id == user_id)
    ).scalar_one_or_none()
    if nowlez_tier in _PAID_NOWLEZ_TIERS:
        active_sub_id = session.execute(
            select(Subscription.id)
            .where(Subscription.user_id == user_id)
            .where(Subscription.status.in_(
                ("trialing", "active", "past_due"),
            ))
            .limit(1)
        ).first()
        if active_sub_id is not None:
            session.add(AuditLog(
                event_type="nowlez.trial_fallback_noop",
                user_id=user_id,
                source="nowlez",
                metadata_={
                    "reason": "user_has_paid_tier",
                    "tier": nowlez_tier,
                },
            ))
            session.flush()
            billing_nowlez_trial_fallback_noop_total.labels(
                reason="user_has_paid_tier",
            ).inc()
            return
        # Tier is set but no active subscription — this is the
        # post-cancellation fallback path. Fall through to the normal
        # setup so the user keeps service via Munshi.

    # Edge case (b): zero saved cases. Don't open billing — there's
    # nothing to bill against — but still acknowledge the trial ended.
    active_case_count = session.execute(
        select(Case.id)
        .where(Case.user_id == user_id)
        .where(Case.refresh_enabled.is_(True))
        .limit(1)
    ).first()
    if active_case_count is None:
        phone = session.execute(
            select(User.phone).where(User.id == user_id)
        ).scalar_one_or_none()
        if phone:
            await send_template_fn(
                to=phone,
                template="nowlez_trial_ended_no_billing_v1",
                variables={},
                brand="nowlez",
            )
        session.add(AuditLog(
            event_type="nowlez.trial_fallback_noop",
            user_id=user_id,
            source="nowlez",
            metadata_={"reason": "no_active_cases"},
        ))
        session.flush()
        billing_nowlez_trial_fallback_noop_total.labels(
            reason="no_active_cases",
        ).inc()
        return

    # 1. Munshi extension (no-op if already exists). The reused-extension
    # case (operator created users_munshi via Munshi-only signup before
    # the day-31 cron) is the same code path: we preserve the existing
    # billing_anniversary_date rather than overwriting with today.
    munshi = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one_or_none()
    if munshi is None:
        session.add(
            UserMunshi(user_id=user_id, billing_anniversary_date=today)
        )
        session.flush()

    # 2. Open billing periods for every active case (ordered oldest-first
    # so we can cap the slice deterministically).
    active_cases = session.execute(
        select(Case)
        .where(Case.user_id == user_id)
        .where(Case.refresh_enabled.is_(True))
        .order_by(asc(Case.created_at), asc(Case.id))
    ).scalars().all()

    for case in active_cases:
        # Idempotent insert — skip if an active period already exists.
        already_open = session.execute(
            select(CaseBillingPeriod.id)
            .where(CaseBillingPeriod.case_id == case.id)
            .where(CaseBillingPeriod.period_end.is_(None))
            .limit(1)
        ).first()
        if already_open is not None:
            continue
        session.add(
            CaseBillingPeriod(
                user_id=user_id, case_id=case.id,
                period_start=now, period_end=None,
            )
        )
    session.flush()

    # 3. 200-case cap: pause the *oldest* cases above the cap.
    if len(active_cases) > NOWLEZ_FALLBACK_CASE_CAP:
        # `active_cases` is ordered oldest-first; the "oldest" are the
        # cases at the *front* of the list. We pause those and keep the
        # newest 200.
        to_pause = active_cases[: len(active_cases) - NOWLEZ_FALLBACK_CASE_CAP]
        pause_ids = [c.id for c in to_pause]
        session.execute(
            update(Case)
            .where(Case.id.in_(pause_ids))
            .values(refresh_enabled=False)
        )
        # Close the billing periods we just opened for them.
        session.execute(
            update(CaseBillingPeriod)
            .where(CaseBillingPeriod.case_id.in_(pause_ids))
            .where(CaseBillingPeriod.period_end.is_(None))
            .values(period_end=now)
        )
    session.flush()

    # 4. Send the fallback template.
    phone = session.execute(
        select(User.phone).where(User.id == user_id)
    ).scalar_one_or_none()
    if phone:
        await send_template_fn(
            to=phone,
            template="nowlez_trial_fallback_v1",
            variables={"case_count": min(
                len(active_cases), NOWLEZ_FALLBACK_CASE_CAP
            )},
            brand="nowlez",
        )

    # 5. Audit log.
    session.add(AuditLog(
        event_type="nowlez.trial_fallback_to_munshi",
        user_id=user_id,
        source="nowlez",
        metadata_={
            "active_cases": len(active_cases),
            "capped_at": NOWLEZ_FALLBACK_CASE_CAP,
        },
    ))
    session.flush()

    # Metric: the happy-path fallback counter. Paired with the freeze
    # counter so the dashboard can show the policy distribution.
    billing_nowlez_trial_fallback_munshi_total.inc()


async def freeze_account(
    user_id: uuid.UUID,
    session: Session,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> None:
    """Hard-stop fallback: disable refresh on every case, notify, audit.

    Used when the operator sets ``BillingConfig.nowlez_lapsed_trial_action='freeze_account'``
    instead of the default Munshi fallback.

    Task 14 edge case: if the user already picked a paid tier AND
    holds an active subscription (raced selection), the freeze is a
    no-op (logged for triage) — we must not disable cases for a user
    who is now actively paying for Nowlez. As with `fallback_to_munshi`,
    a tier-set + cancelled-subscription combination is the post-cancel
    hand-off path and freeze proceeds normally.

    Leaves ``users_nowlez.tier=None`` explicitly so the user can pick a
    tier later and be reactivated via the standard subscription flow.
    """
    from data_access.models.audit import AuditLog
    from data_access.models.billing import (
        CaseBillingPeriod, Subscription,
    )
    from data_access.models.case import Case
    from data_access.models.user import User, UserNowlez

    now = datetime.now(timezone.utc)

    # Edge case: user raced into a paid tier WITH active subscription.
    nowlez = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one_or_none()
    if nowlez is not None and nowlez.tier in _PAID_NOWLEZ_TIERS:
        active_sub_id = session.execute(
            select(Subscription.id)
            .where(Subscription.user_id == user_id)
            .where(Subscription.status.in_(
                ("trialing", "active", "past_due"),
            ))
            .limit(1)
        ).first()
        if active_sub_id is not None:
            session.add(AuditLog(
                event_type="nowlez.trial_freeze_noop",
                user_id=user_id,
                source="nowlez",
                metadata_={
                    "reason": "user_has_paid_tier",
                    "tier": nowlez.tier,
                },
            ))
            session.flush()
            billing_nowlez_trial_fallback_noop_total.labels(
                reason="user_has_paid_tier",
            ).inc()
            return
        # Otherwise tier is set but cancelled — proceed to freeze.

    # Belt-and-braces: explicitly null the tier so any half-set state
    # (e.g. tier='free' legacy) resolves to "no Nowlez access".
    if nowlez is not None and nowlez.tier is not None:
        nowlez.tier = None
    session.execute(
        update(Case)
        .where(Case.user_id == user_id)
        .values(refresh_enabled=False)
    )
    session.execute(
        update(CaseBillingPeriod)
        .where(CaseBillingPeriod.user_id == user_id)
        .where(CaseBillingPeriod.period_end.is_(None))
        .values(period_end=now)
    )
    phone = session.execute(
        select(User.phone).where(User.id == user_id)
    ).scalar_one_or_none()
    if phone:
        await send_template_fn(
            to=phone,
            template="nowlez_trial_fallback_v1",
            variables={"case_count": 0},
            brand="nowlez",
        )
    session.add(AuditLog(
        event_type="nowlez.trial_frozen",
        user_id=user_id,
        source="nowlez",
        metadata_={"reason": "trial_lapsed_no_tier"},
    ))
    session.flush()

    # Metric counterpart to billing_nowlez_trial_fallback_munshi_total.
    billing_nowlez_trial_fallback_freeze_total.inc()


async def apply_lapsed_trial_action(
    user_id: uuid.UUID,
    session: Session,
    send_template_fn: Callable[..., Awaitable[Any]],
    *,
    action: str,
) -> str:
    """Dispatch to the configured lapsed-trial action (Task 14 entry point).

    The day-31 cron passes ``BillingConfig.nowlez_lapsed_trial_action``
    here so the choice of policy lives in config rather than code:

    * ``'fallback_to_munshi'`` (default) →
      :func:`fallback_to_munshi`
    * ``'freeze_account'`` →
      :func:`freeze_account`

    Returns the action string that was actually executed (helpful for
    the cron's structured log line).

    Raises:
        :class:`ValueError`: when ``action`` is not one of the
            recognised constants — caller should treat this as a config
            error and refuse to start.
    """
    if action == LAPSED_TRIAL_ACTION_FALLBACK:
        await fallback_to_munshi(user_id, session, send_template_fn)
        return LAPSED_TRIAL_ACTION_FALLBACK
    if action == LAPSED_TRIAL_ACTION_FREEZE:
        await freeze_account(user_id, session, send_template_fn)
        return LAPSED_TRIAL_ACTION_FREEZE
    raise ValueError(
        f"Unknown nowlez_lapsed_trial_action {action!r}; expected one of "
        f"{sorted(_VALID_LAPSED_ACTIONS)}"
    )


__all__ = [
    "fallback_to_munshi",
    "freeze_account",
    "apply_lapsed_trial_action",
    "NOWLEZ_FALLBACK_CASE_CAP",
    "LAPSED_TRIAL_ACTION_FALLBACK",
    "LAPSED_TRIAL_ACTION_FREEZE",
]
