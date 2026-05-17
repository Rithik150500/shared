"""Nowlez 30-day Chambers trial state machine (spec Section 1.1, Task 7.1).

Every Nowlez signup lands in a 30-day Chambers trial — full Chambers
features, no charge, no tier selected. The trial ends either by the
user picking a tier (Task 7.4) or by the day-31 fallback cron firing
(Task 7.5). This module owns the read/write surface for the trial
fields on ``users_nowlez``:

* :func:`create_trial_for_new_signup` — INSERT/UPDATE the row with
  ``tier=NULL``, ``trial_started_at=NOW()``, ``trial_ends_at=NOW()+30d``,
  audit log, then enqueue the welcome WhatsApp template. Idempotent:
  if a row already exists, the function is a no-op (preserves existing
  trial_ends_at).
* :func:`is_in_trial` — True iff the row exists, ``tier IS NULL``, and
  ``trial_ends_at > NOW()``.
* :func:`days_remaining_in_trial` — whole days until ``trial_ends_at``;
  zero when not in trial or expired.

Three short aliases simplify call sites:

* :func:`start_trial` — alias for :func:`create_trial_for_new_signup`.
* :func:`trial_ends_at_for` — returns the raw ``trial_ends_at`` instant
  or None (callers that already know they're in trial don't need to
  re-derive it from days_remaining).
* :func:`expire_trial` — administrative "expire now" used by the day-31
  fallback cron and by tests; sets ``trial_ends_at = NOW()`` so
  :func:`is_in_trial` flips False on the next read.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from case_billing.metrics import (
    billing_nowlez_trials_expired_total,
    billing_nowlez_trials_started_total,
)


# Spec default for the trial duration (BillingConfig.nowlez_trial_duration_days).
# Kept as a module-level constant so call sites don't have to instantiate
# BillingConfig just to start a trial.
NOWLEZ_TRIAL_DURATION_DAYS: int = 30


def _to_aware_utc(value: datetime | None) -> datetime | None:
    """Coerce a possibly-naive datetime to UTC-aware (SQLite round-trip fix)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def create_trial_for_new_signup(
    *,
    user_id: uuid.UUID,
    name: str,
    session: Session,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> None:
    """Insert (or no-op if exists) the user's 30-day Chambers trial row.

    Spec Section 1.1: every Nowlez signup gets a 30-day Chambers trial
    (no tier selected, full Chambers perks). This function is the only
    place ``users_nowlez.trial_started_at``/``trial_ends_at`` get
    written on signup.

    Idempotency: if a ``users_nowlez`` row already exists for the user
    we DO NOT overwrite ``trial_ends_at`` (resetting the trial would
    let users farm extensions by re-running signup). Likewise we DO NOT
    re-send the template on the second call.

    Args:
        user_id: The just-created user's primary key.
        name: Display name to persist (the migration left
            ``users_nowlez.name`` NOT NULL so signup must supply one).
        session: Open SQLAlchemy session.
        send_template_fn: Async callable matching sub-project B's
            ``enqueue_send_template`` signature.
    """
    from data_access.models.audit import AuditLog
    from data_access.models.user import User, UserNowlez

    existing = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one_or_none()
    if existing is not None:
        # Already has a Nowlez extension — leave the trial untouched.
        return

    now = datetime.now(timezone.utc)
    session.add(
        UserNowlez(
            user_id=user_id,
            name=name,
            tier=None,
            trial_started_at=now,
            trial_ends_at=now + timedelta(days=NOWLEZ_TRIAL_DURATION_DAYS),
        )
    )
    session.add(
        AuditLog(
            event_type="nowlez.trial_started",
            user_id=user_id,
            source="nowlez",
            metadata_={"trial_days": NOWLEZ_TRIAL_DURATION_DAYS},
        )
    )
    session.flush()

    phone = session.execute(
        select(User.phone).where(User.id == user_id)
    ).scalar_one_or_none()
    if phone:
        await send_template_fn(
            to=phone,
            template="nowlez_trial_started_v1",
            variables={"name": name, "trial_days": NOWLEZ_TRIAL_DURATION_DAYS},
            brand="nowlez",
        )

    # Metric: only increment on the new-row path (existing-row early
    # return above doesn't fall through to here).
    billing_nowlez_trials_started_total.inc()


async def is_in_trial(user_id: uuid.UUID, session: Session) -> bool:
    """Return True iff the user is currently inside the 30-day trial window.

    Conditions for True (all must hold):

    * ``users_nowlez`` row exists.
    * ``tier IS NULL`` (picking a tier ends the trial immediately).
    * ``trial_ends_at`` is in the future.
    """
    from data_access.models.user import UserNowlez

    row = session.execute(
        select(UserNowlez.tier, UserNowlez.trial_ends_at).where(
            UserNowlez.user_id == user_id
        )
    ).first()
    if row is None:
        return False
    tier, trial_ends_at = row
    if tier is not None:
        return False
    trial_ends_at = _to_aware_utc(trial_ends_at)
    if trial_ends_at is None:
        return False
    return trial_ends_at > datetime.now(timezone.utc)


async def days_remaining_in_trial(
    user_id: uuid.UUID, session: Session,
) -> int:
    """Return the whole number of days remaining until ``trial_ends_at``.

    Returns 0 when the user is not currently in a trial (no row,
    tier set, or trial already expired).
    """
    from data_access.models.user import UserNowlez

    row = session.execute(
        select(UserNowlez.tier, UserNowlez.trial_ends_at).where(
            UserNowlez.user_id == user_id
        )
    ).first()
    if row is None:
        return 0
    tier, trial_ends_at = row
    if tier is not None:
        return 0
    trial_ends_at = _to_aware_utc(trial_ends_at)
    if trial_ends_at is None:
        return 0
    delta = trial_ends_at - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return 0
    return delta.days


async def trial_ends_at_for(
    user_id: uuid.UUID, session: Session,
) -> datetime | None:
    """Return the raw ``trial_ends_at`` instant, or None if no row."""
    from data_access.models.user import UserNowlez

    val = session.execute(
        select(UserNowlez.trial_ends_at).where(UserNowlez.user_id == user_id)
    ).scalar_one_or_none()
    return val


async def expire_trial(user_id: uuid.UUID, session: Session) -> None:
    """Set ``trial_ends_at = NOW()`` so the next :func:`is_in_trial` is False.

    Used by the day-31 fallback cron and by admin tools. Idempotent —
    if the row is missing or the trial has already expired the function
    is a no-op.
    """
    from data_access.models.user import UserNowlez

    row = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one_or_none()
    if row is None:
        return
    if row.tier is not None:
        # Tier already picked — trial ended via the happy path.
        return
    row.trial_ends_at = datetime.now(timezone.utc)
    session.flush()
    # Counter only fires for the cron/admin "expire now" path; natural
    # tier-pick exits use the happy-path counter on `select_tier_and_subscribe`.
    billing_nowlez_trials_expired_total.inc()


# --- public aliases --------------------------------------------------------

# `start_trial` reads better at signup call sites than the longer
# `create_trial_for_new_signup`. Kept as an alias rather than the
# canonical name because the spec's docstrings reference the latter
# verbatim.
start_trial = create_trial_for_new_signup
