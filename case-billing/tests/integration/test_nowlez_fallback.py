"""Day-31 fallback to Munshi tests (Task 7.5.2).

`fallback_to_munshi(user_id, session, send_template_fn)` runs on day 31
when the trial has expired and the user has not picked a tier:

  today_ist = date.today()
  INSERT INTO users_munshi (user_id, billing_anniversary_date=today)
       ON CONFLICT DO NOTHING
  FOR each active case with refresh_enabled=True:
      INSERT case_billing_periods (period_start=NOW(), period_end=NULL)
  IF user has > 200 active cases:
      pause oldest down to 200 (refresh_enabled=False; close periods)
  send `nowlez_trial_fallback_v1` template
  audit log 'nowlez.trial_fallback_to_munshi'

`freeze_account` is the alternative path (BillingConfig
`nowlez_lapsed_trial_action='freeze_account'`).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.nowlez.fallback import (
    fallback_to_munshi,
    freeze_account,
)


@pytest.fixture()
def session() -> Session:
    from data_access.base import Base

    import data_access.models.auth  # noqa: F401
    import data_access.models.billing  # noqa: F401
    import data_access.models.case  # noqa: F401
    import data_access.models.user  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _make_expired_trial_user(session: Session) -> uuid.UUID:
    from data_access.models.user import User, UserNowlez

    user = User(phone=f"+91999{uuid.uuid4().hex[:7]}")
    session.add(user)
    session.flush()
    session.add(
        UserNowlez(
            user_id=user.id, name="Trial",
            tier=None,
            trial_started_at=datetime.now(timezone.utc) - timedelta(days=31),
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    session.flush()
    return user.id


def _add_cases(
    session: Session,
    user_id: uuid.UUID,
    n: int,
    *,
    refresh_enabled: bool = True,
) -> list[uuid.UUID]:
    from data_access.models.case import Case

    ids = []
    for _ in range(n):
        c = Case(
            user_id=user_id, cnr=uuid.uuid4().hex[:16],
            portal="district", refresh_enabled=refresh_enabled,
        )
        session.add(c)
        session.flush()
        ids.append(c.id)
    return ids


# --- fallback_to_munshi ----------------------------------------------------


def test_fallback_creates_munshi_extension_and_opens_periods(
    session: Session,
) -> None:
    user_id = _make_expired_trial_user(session)
    _add_cases(session, user_id, n=50)

    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.user import UserMunshi, UserNowlez

    munshi = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one()
    assert munshi.billing_anniversary_date is not None

    periods = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.user_id == user_id)
    ).scalars().all()
    assert len(periods) == 50

    sender.assert_called_once()
    assert sender.call_args.kwargs["template"] == "nowlez_trial_fallback_v1"


def test_fallback_caps_at_200_cases(session: Session) -> None:
    user_id = _make_expired_trial_user(session)
    _add_cases(session, user_id, n=250)

    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.case import Case

    # Only 200 cases remain active.
    active = session.execute(
        select(Case)
        .where(Case.user_id == user_id)
        .where(Case.refresh_enabled.is_(True))
    ).scalars().all()
    assert len(active) == 200

    paused = session.execute(
        select(Case)
        .where(Case.user_id == user_id)
        .where(Case.refresh_enabled.is_(False))
    ).scalars().all()
    assert len(paused) == 50

    # 200 case_billing_periods created (one per active case).
    open_periods = session.execute(
        select(CaseBillingPeriod)
        .where(CaseBillingPeriod.user_id == user_id)
        .where(CaseBillingPeriod.period_end.is_(None))
    ).scalars().all()
    assert len(open_periods) == 200


def test_fallback_keeps_tier_null(session: Session) -> None:
    user_id = _make_expired_trial_user(session)
    _add_cases(session, user_id, n=10)
    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    from data_access.models.user import UserNowlez
    nowlez = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one()
    assert nowlez.tier is None


def test_fallback_idempotent_on_existing_munshi_extension(
    session: Session,
) -> None:
    """If users_munshi already exists, do not overwrite billing_anniversary_date."""
    from data_access.models.user import UserMunshi

    user_id = _make_expired_trial_user(session)
    _add_cases(session, user_id, n=3)
    session.add(UserMunshi(
        user_id=user_id, billing_anniversary_date=date(2025, 1, 1),
    ))
    session.flush()

    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    munshi = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one()
    assert munshi.billing_anniversary_date == date(2025, 1, 1)


# --- freeze_account --------------------------------------------------------


def test_freeze_account_disables_refresh_and_sends_template(
    session: Session,
) -> None:
    user_id = _make_expired_trial_user(session)
    _add_cases(session, user_id, n=10)

    sender = AsyncMock()
    _run(freeze_account(user_id, session, send_template_fn=sender))
    session.flush()

    from data_access.models.case import Case
    cases = session.execute(
        select(Case).where(Case.user_id == user_id)
    ).scalars().all()
    assert all(c.refresh_enabled is False for c in cases)
    sender.assert_called_once()
    assert sender.call_args.kwargs["template"] == "nowlez_trial_fallback_v1"
