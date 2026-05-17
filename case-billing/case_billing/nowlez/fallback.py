"""Day-31 fallback paths for trial-lapsed Nowlez users (Task 7.5).

Two policies, switchable per ``BillingConfig.nowlez_lapsed_trial_action``:

* :func:`fallback_to_munshi` (default) — graceful: create a Munshi
  extension for the user, open billing periods for their active cases,
  pause anything over 200 cases (oldest first), and notify. The user
  keeps service but starts paying Munshi-style postpaid.

* :func:`freeze_account` — hard stop: set every case to
  ``refresh_enabled=False``, notify. Used when the operator wants to
  withhold service until the user manually picks a tier.

Both paths leave ``users_nowlez.tier`` as ``NULL`` so the user can still
pick a tier later and be upgraded back to a subscription. They both
write an audit log entry so the cron is observable.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import asc, select, update
from sqlalchemy.orm import Session


# Sub-project E spec: cap at 200 cases when falling back from Nowlez to
# Munshi (spec Section 6.4 — "User has 200+ cases when falling back").
NOWLEZ_FALLBACK_CASE_CAP: int = 200


async def fallback_to_munshi(
    user_id: uuid.UUID,
    session: Session,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> None:
    """Convert a lapsed-trial Nowlez user into a Munshi postpaid user.

    Side-effects (in order):

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
    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.case import Case
    from data_access.models.user import User, UserMunshi

    now = datetime.now(timezone.utc)
    today = date.today()

    # 1. Munshi extension (no-op if already exists).
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


async def freeze_account(
    user_id: uuid.UUID,
    session: Session,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> None:
    """Hard-stop fallback: disable refresh on every case, notify, audit.

    Used when the operator sets ``BillingConfig.nowlez_lapsed_trial_action='freeze_account'``
    instead of the default Munshi fallback.
    """
    from data_access.models.audit import AuditLog
    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.case import Case
    from data_access.models.user import User

    now = datetime.now(timezone.utc)
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
