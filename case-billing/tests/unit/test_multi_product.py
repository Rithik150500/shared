"""Tests for the cross-product Munshi billing gate (Task 13).

`should_charge_munshi_for_this_case(user_id, session)` wraps the lower-
level Munshi eligibility predicate with explicit Nowlez overrides so
the cron's intent is observable at the call site:

    if not should_charge_munshi_for_this_case(uid, session):
        return  # Nowlez covers this user, or they're suspended

The wrapper returns False when:
* user lacks a Munshi extension or is inactive (delegated to eligibility),
* user holds an active paid Nowlez tier,
* user is inside an active Nowlez trial window,
* the user's most recent Munshi invoice is in ``suspended`` status.

It returns True only for a Munshi-only, active, non-suspended user.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from case_billing.shared.multi_product import (
    should_charge_munshi_for_this_case,
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


def _make_user(session: Session, *, is_active: bool = True) -> uuid.UUID:
    from data_access.models.user import User

    user = User(
        phone=f"+91999{uuid.uuid4().hex[:7]}", is_active=is_active,
    )
    session.add(user)
    session.flush()
    return user.id


def _add_munshi_extension(session: Session, user_id: uuid.UUID) -> None:
    from data_access.models.user import UserMunshi

    session.add(UserMunshi(user_id=user_id))
    session.flush()


def _add_nowlez_paid(
    session: Session, user_id: uuid.UUID, tier: str = "chambers",
) -> None:
    from data_access.models.user import UserNowlez

    session.add(UserNowlez(user_id=user_id, name="Paid", tier=tier))
    session.flush()


def _add_nowlez_trial(
    session: Session,
    user_id: uuid.UUID,
    *,
    days_remaining: int = 5,
) -> None:
    from data_access.models.user import UserNowlez

    session.add(
        UserNowlez(
            user_id=user_id, name="Trialing",
            tier=None,
            trial_started_at=datetime.now(timezone.utc) - timedelta(
                days=30 - days_remaining,
            ),
            trial_ends_at=datetime.now(timezone.utc) + timedelta(
                days=days_remaining,
            ),
        )
    )
    session.flush()


def _add_nowlez_lapsed(session: Session, user_id: uuid.UUID) -> None:
    """Lapsed trial — Nowlez row exists with tier=NULL but trial expired."""
    from data_access.models.user import UserNowlez

    session.add(
        UserNowlez(
            user_id=user_id, name="Lapsed",
            tier=None,
            trial_started_at=datetime.now(timezone.utc) - timedelta(days=40),
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
    )
    session.flush()


def _add_suspended_invoice(session: Session, user_id: uuid.UUID) -> None:
    from data_access.models.billing import MunshiInvoice

    session.add(
        MunshiInvoice(
            user_id=user_id,
            razorpay_invoice_id=f"inv_{uuid.uuid4().hex[:10]}",
            cycle_start=datetime(2026, 3, 1, tzinfo=timezone.utc),
            cycle_end=datetime(2026, 4, 1, tzinfo=timezone.utc),
            case_count=10, amount_paise=10000,
            status="suspended",
            due_at=datetime(2026, 4, 8, tzinfo=timezone.utc),
        )
    )
    session.flush()


# --- positive cases --------------------------------------------------------


def test_munshi_only_user_should_be_charged(session: Session) -> None:
    user_id = _make_user(session)
    _add_munshi_extension(session, user_id)
    assert _run(should_charge_munshi_for_this_case(user_id, session)) is True


def test_munshi_user_with_lapsed_nowlez_trial_should_be_charged(
    session: Session,
) -> None:
    """A user whose trial expired without picking a tier (and no fallback
    yet) still has tier=NULL on users_nowlez, but the trial window is in
    the past — Munshi should bill them."""
    user_id = _make_user(session)
    _add_munshi_extension(session, user_id)
    _add_nowlez_lapsed(session, user_id)
    assert _run(should_charge_munshi_for_this_case(user_id, session)) is True


# --- negative cases --------------------------------------------------------


def test_paid_nowlez_user_should_not_be_charged(session: Session) -> None:
    user_id = _make_user(session)
    _add_munshi_extension(session, user_id)
    _add_nowlez_paid(session, user_id, tier="chambers")
    assert _run(should_charge_munshi_for_this_case(user_id, session)) is False


@pytest.mark.parametrize("tier", ["advocate", "counsel", "chambers"])
def test_each_paid_tier_suppresses_munshi(
    session: Session, tier: str,
) -> None:
    user_id = _make_user(session)
    _add_munshi_extension(session, user_id)
    _add_nowlez_paid(session, user_id, tier=tier)
    assert _run(should_charge_munshi_for_this_case(user_id, session)) is False


def test_active_trial_user_should_not_be_charged(session: Session) -> None:
    user_id = _make_user(session)
    _add_munshi_extension(session, user_id)
    _add_nowlez_trial(session, user_id, days_remaining=15)
    assert _run(should_charge_munshi_for_this_case(user_id, session)) is False


def test_trial_expiring_today_still_suppresses(session: Session) -> None:
    """A trial with trial_ends_at = now + 1 minute is still active."""
    user_id = _make_user(session)
    _add_munshi_extension(session, user_id)
    from data_access.models.user import UserNowlez

    session.add(
        UserNowlez(
            user_id=user_id, name="Edge",
            tier=None,
            trial_started_at=datetime.now(timezone.utc) - timedelta(days=29),
            trial_ends_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
    )
    session.flush()
    assert _run(should_charge_munshi_for_this_case(user_id, session)) is False


def test_inactive_user_should_not_be_charged(session: Session) -> None:
    user_id = _make_user(session, is_active=False)
    _add_munshi_extension(session, user_id)
    assert _run(should_charge_munshi_for_this_case(user_id, session)) is False


def test_user_without_munshi_extension_should_not_be_charged(
    session: Session,
) -> None:
    user_id = _make_user(session)
    # No UserMunshi row.
    assert _run(should_charge_munshi_for_this_case(user_id, session)) is False


def test_suspended_invoice_blocks_further_charge(session: Session) -> None:
    user_id = _make_user(session)
    _add_munshi_extension(session, user_id)
    _add_suspended_invoice(session, user_id)
    assert _run(should_charge_munshi_for_this_case(user_id, session)) is False


def test_missing_user_returns_false(session: Session) -> None:
    """A user_id with no users row should yield False, never raise."""
    bogus = uuid.uuid4()
    assert _run(should_charge_munshi_for_this_case(bogus, session)) is False
