"""Cross-product eligibility predicates (spec Section 2.3 + 6.2).

A user can sit in one of seven multi-product states (Munshi-only, paid
Nowlez, trial Nowlez, both, trial+Munshi, lapsed trial, suspended) and
the choice of which billing engine fires this month depends entirely on
the combination. This module centralises that decision so neither
the Munshi cron nor the Nowlez webhook handler has to re-derive it.

Three top-level predicates are exported:

* :func:`is_user_eligible_for_munshi_billing` — the master gate that the
  Munshi cron consults before generating an anniversary invoice. Inverse
  of "paying Nowlez subscriber", AND has a Munshi extension row, AND
  not currently in a Nowlez trial, AND not already suspended.
* :func:`has_nowlez_access` — True for paid OR trialing Nowlez users.
  Used by feature gates outside billing.
* :func:`is_paying_nowlez_subscriber` — narrow check restricted to one
  of the three paying tiers (advocate / counsel / chambers).
* :func:`effective_tier` — returns the tier the user has *for feature
  purposes right now*. Trial users get ``'chambers'`` (full tier perks
  during the 30-day promo); paid users get their actual tier.

All predicates are ``async def`` for API uniformity with the rest of
the package, even though they only execute synchronous SQLAlchemy
SELECTs against the local session.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session


# Tiers that count as "paid subscriber" for the Nowlez side. Anything
# outside this set (NULL, 'free') means the user is *not* on a paying
# Nowlez plan; the `free` legacy default lingers on rows pre-migration.
_PAID_TIERS: frozenset[str] = frozenset({"advocate", "counsel", "chambers"})


def _trial_is_active(trial_ends_at: datetime | None) -> bool:
    """Return True iff ``trial_ends_at`` is in the future.

    Coerces naive datetimes (as emitted by SQLite) to UTC before comparing
    so the predicate works across Postgres (timezone-aware) and SQLite
    (timezone-naive) sessions.
    """
    if trial_ends_at is None:
        return False
    if trial_ends_at.tzinfo is None:
        # SQLite stripped the tzinfo on the round-trip; assume UTC, which
        # matches the production write contract (`now()` UTC instants).
        trial_ends_at = trial_ends_at.replace(tzinfo=timezone.utc)
    return trial_ends_at > datetime.now(timezone.utc)


async def has_nowlez_access(user_id: uuid.UUID, session: Session) -> bool:
    """Return True iff the user has *any* form of active Nowlez access.

    Includes both paid tier holders and users still inside their 30-day
    Chambers trial. Used by feature-flag gates outside billing — for
    billing decisions use :func:`is_paying_nowlez_subscriber` instead.
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
    if tier in _PAID_TIERS:
        return True
    if _trial_is_active(trial_ends_at):
        return True
    return False


async def is_paying_nowlez_subscriber(
    user_id: uuid.UUID, session: Session,
) -> bool:
    """Return True iff the user holds one of the three paid Nowlez tiers.

    A NULL tier (trial user or never selected) or the legacy ``'free'``
    tier both return False.
    """
    from data_access.models.user import UserNowlez

    tier = session.execute(
        select(UserNowlez.tier).where(UserNowlez.user_id == user_id)
    ).scalar_one_or_none()
    return tier in _PAID_TIERS


async def effective_tier(
    user_id: uuid.UUID, session: Session,
) -> str | None:
    """Return the tier the user is entitled to *right now*, or None.

    During the 30-day Chambers trial (tier IS NULL, trial_ends_at in
    future), the function returns ``'chambers'`` because trial users get
    full Chambers perks for feature gating. After the trial expires
    with no tier selected, returns None (no access).
    """
    from data_access.models.user import UserNowlez

    row = session.execute(
        select(UserNowlez.tier, UserNowlez.trial_ends_at).where(
            UserNowlez.user_id == user_id
        )
    ).first()
    if row is None:
        return None
    tier, trial_ends_at = row
    if tier in _PAID_TIERS:
        return tier
    # Trial: NULL tier + active trial window → grant Chambers tier perks.
    if tier is None and _trial_is_active(trial_ends_at):
        return "chambers"
    return None


async def is_user_eligible_for_munshi_billing(
    user_id: uuid.UUID, session: Session,
) -> bool:
    """Return True iff the Munshi cron should issue an invoice this cycle.

    Mirrors spec Section 2.3 with explicit short-circuits:

    1. User row missing or inactive → False.
    2. No Munshi extension row → False.
    3. Currently a paying Nowlez subscriber → False (Nowlez bills instead).
    4. Currently inside a Nowlez trial window → False (trial covers all).
    5. The user's most recent Munshi invoice is `suspended` → False.
    6. Otherwise → True.
    """
    from data_access.models.billing import MunshiInvoice
    from data_access.models.user import User, UserMunshi, UserNowlez

    # 1. user active?
    is_active = session.execute(
        select(User.is_active).where(User.id == user_id)
    ).scalar_one_or_none()
    if not is_active:  # also handles missing user_id
        return False

    # 2. has Munshi extension?
    has_munshi = session.execute(
        select(UserMunshi.user_id).where(UserMunshi.user_id == user_id)
    ).first()
    if has_munshi is None:
        return False

    # 3/4. Nowlez paying or in-trial → suppress Munshi.
    nowlez_row = session.execute(
        select(UserNowlez.tier, UserNowlez.trial_ends_at).where(
            UserNowlez.user_id == user_id
        )
    ).first()
    if nowlez_row is not None:
        tier, trial_ends_at = nowlez_row
        if tier in _PAID_TIERS:
            return False
        if _trial_is_active(trial_ends_at):
            return False

    # 5. latest invoice suspended → False.
    last_status = session.execute(
        select(MunshiInvoice.status)
        .where(MunshiInvoice.user_id == user_id)
        .order_by(MunshiInvoice.cycle_end.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last_status == "suspended":
        return False

    return True
