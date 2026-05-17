"""Nowlez trial state-machine unit tests (Task 7.1.2).

Covers `create_trial_for_new_signup`, `is_in_trial`,
`days_remaining_in_trial`, and the convenience aliases the public API
re-exports (`start_trial`, `trial_ends_at_for`, `expire_trial`).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.nowlez.trial import (
    create_trial_for_new_signup,
    days_remaining_in_trial,
    expire_trial,
    is_in_trial,
    start_trial,
    trial_ends_at_for,
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


def _make_user(session: Session) -> uuid.UUID:
    from data_access.models.user import User

    u = User(phone=f"+91999{uuid.uuid4().hex[:7]}")
    session.add(u)
    session.flush()
    return u.id


# --- create_trial_for_new_signup ------------------------------------------


def test_create_trial_inserts_nowlez_row_with_30_day_window(
    session: Session,
) -> None:
    user_id = _make_user(session)
    sender = AsyncMock()

    _run(create_trial_for_new_signup(
        user_id=user_id, name="Test User", session=session,
        send_template_fn=sender,
    ))
    session.flush()

    from data_access.models.user import UserNowlez
    row = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one()
    assert row.tier is None
    assert row.trial_started_at is not None
    assert row.trial_ends_at is not None
    delta = row.trial_ends_at - row.trial_started_at
    # 30 days ± an hour for rounding.
    assert abs(delta - timedelta(days=30)) < timedelta(hours=1)

    sender.assert_called_once()
    assert sender.call_args.kwargs["template"] == "nowlez_trial_started_v1"


def test_create_trial_is_idempotent_for_existing_row(session: Session) -> None:
    """Calling twice doesn't shift trial_ends_at on the existing row."""
    user_id = _make_user(session)
    sender = AsyncMock()
    _run(create_trial_for_new_signup(
        user_id=user_id, name="Test", session=session, send_template_fn=sender,
    ))
    session.flush()
    from data_access.models.user import UserNowlez
    first = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one()
    first_ends = first.trial_ends_at

    _run(create_trial_for_new_signup(
        user_id=user_id, name="Test", session=session, send_template_fn=sender,
    ))
    session.flush()

    second = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one()
    assert second.trial_ends_at == first_ends


# --- is_in_trial ----------------------------------------------------------


def test_is_in_trial_false_without_nowlez_row(session: Session) -> None:
    user_id = _make_user(session)
    assert _run(is_in_trial(user_id, session)) is False


def test_is_in_trial_true_when_trial_ends_in_future(session: Session) -> None:
    user_id = _make_user(session)
    sender = AsyncMock()
    _run(create_trial_for_new_signup(
        user_id=user_id, name="Test", session=session, send_template_fn=sender,
    ))
    session.flush()
    assert _run(is_in_trial(user_id, session)) is True


def test_is_in_trial_false_when_trial_expired(session: Session) -> None:
    from data_access.models.user import UserNowlez
    user_id = _make_user(session)
    session.add(
        UserNowlez(
            user_id=user_id,
            name="Test",
            tier=None,
            trial_started_at=datetime.now(timezone.utc) - timedelta(days=40),
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
    )
    session.flush()
    assert _run(is_in_trial(user_id, session)) is False


def test_is_in_trial_false_when_tier_selected(session: Session) -> None:
    """Even if trial_ends_at is in future, a non-null tier ends the trial."""
    from data_access.models.user import UserNowlez
    user_id = _make_user(session)
    session.add(
        UserNowlez(
            user_id=user_id, name="Test", tier="chambers",
            trial_started_at=datetime.now(timezone.utc),
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=10),
        )
    )
    session.flush()
    assert _run(is_in_trial(user_id, session)) is False


# --- days_remaining_in_trial ----------------------------------------------


def test_days_remaining_zero_for_non_trial_user(session: Session) -> None:
    user_id = _make_user(session)
    assert _run(days_remaining_in_trial(user_id, session)) == 0


def test_days_remaining_within_window(session: Session) -> None:
    from data_access.models.user import UserNowlez
    user_id = _make_user(session)
    now = datetime.now(timezone.utc)
    session.add(
        UserNowlez(
            user_id=user_id, name="Test", tier=None,
            trial_started_at=now,
            trial_ends_at=now + timedelta(days=27, hours=12),
        )
    )
    session.flush()
    # Rounded down to whole days.
    remaining = _run(days_remaining_in_trial(user_id, session))
    assert 26 <= remaining <= 28


def test_days_remaining_zero_after_expiry(session: Session) -> None:
    from data_access.models.user import UserNowlez
    user_id = _make_user(session)
    session.add(
        UserNowlez(
            user_id=user_id, name="Test", tier=None,
            trial_started_at=datetime.now(timezone.utc) - timedelta(days=40),
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
    )
    session.flush()
    assert _run(days_remaining_in_trial(user_id, session)) == 0


# --- trial_ends_at_for, expire_trial, start_trial (aliases) ----------------


def test_start_trial_is_alias_of_create_trial(session: Session) -> None:
    user_id = _make_user(session)
    sender = AsyncMock()
    _run(start_trial(user_id=user_id, name="Test", session=session, send_template_fn=sender))
    session.flush()
    assert _run(is_in_trial(user_id, session)) is True


def test_trial_ends_at_for_returns_datetime_or_none(session: Session) -> None:
    user_id = _make_user(session)
    assert _run(trial_ends_at_for(user_id, session)) is None

    sender = AsyncMock()
    _run(start_trial(user_id=user_id, name="Test", session=session, send_template_fn=sender))
    session.flush()
    val = _run(trial_ends_at_for(user_id, session))
    assert val is not None
    assert val > datetime.now(timezone.utc).replace(tzinfo=None)


def test_expire_trial_sets_trial_ends_at_to_now(session: Session) -> None:
    user_id = _make_user(session)
    sender = AsyncMock()
    _run(start_trial(user_id=user_id, name="Test", session=session, send_template_fn=sender))
    session.flush()

    _run(expire_trial(user_id, session))
    session.flush()

    assert _run(is_in_trial(user_id, session)) is False
