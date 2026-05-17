"""Multi-product eligibility predicate tests (Task 6.3.2).

Covers all seven user states from spec Section 6.2 explicitly:

    | # | state                  | users_munshi | nowlez tier | trial_ends_at | Billed by         |
    |---|------------------------|--------------|-------------|---------------|-------------------|
    | 1 | Munshi only            | exists       | (no row)    | n/a           | Munshi            |
    | 2 | Nowlez trial           | (no row)     | NULL        | > NOW         | None              |
    | 3 | Nowlez paid            | (no row)     | active tier | n/a           | Nowlez            |
    | 4 | Both active            | exists       | active tier | n/a           | Nowlez (Munshi suppressed) |
    | 5 | Nowlez trial + Munshi  | exists       | NULL        | > NOW         | None during trial |
    | 6 | Nowlez trial lapsed    | exists (D31) | NULL        | < NOW         | Munshi            |
    | 7 | Suspended Munshi       | exists       | n/a         | n/a           | None (suspended)  |

Plus inactive users (`users.is_active=False`) are never billable.

Each predicate test uses an in-memory SQLite session built from the
shared `data_access.base.Base` metadata to match the production schema.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from case_billing.shared.eligibility import (
    effective_tier,
    has_nowlez_access,
    is_paying_nowlez_subscriber,
    is_user_eligible_for_munshi_billing,
)


# ---------- fixtures -------------------------------------------------------


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

    u = User(
        phone=f"+91999{uuid.uuid4().hex[:7]}",
        is_active=is_active,
    )
    session.add(u)
    session.flush()
    return u.id


def _add_munshi_ext(
    session: Session,
    user_id: uuid.UUID,
    anniversary: date | None = None,
) -> None:
    from data_access.models.user import UserMunshi

    if anniversary is None:
        anniversary = date(2026, 3, 15)
    session.add(UserMunshi(user_id=user_id, billing_anniversary_date=anniversary))
    session.flush()


def _add_nowlez_ext(
    session: Session,
    user_id: uuid.UUID,
    *,
    tier: str | None,
    trial_ends_at: datetime | None = None,
    name: str = "Test User",
) -> None:
    from data_access.models.user import UserNowlez

    session.add(
        UserNowlez(
            user_id=user_id,
            name=name,
            tier=tier,
            trial_ends_at=trial_ends_at,
        )
    )
    session.flush()


def _add_munshi_invoice_status(
    session: Session,
    user_id: uuid.UUID,
    *,
    status: str,
    cycle_start: datetime,
    cycle_end: datetime,
) -> None:
    from data_access.models.billing import MunshiInvoice

    session.add(
        MunshiInvoice(
            user_id=user_id,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            case_count=1,
            amount_paise=1000,
            status=status,
        )
    )
    session.flush()


# ---------- has_nowlez_access ---------------------------------------------


def test_has_nowlez_access_false_when_no_row(session: Session) -> None:
    user_id = _make_user(session)
    assert _run(has_nowlez_access(user_id, session)) is False


def test_has_nowlez_access_true_when_paid_tier(session: Session) -> None:
    user_id = _make_user(session)
    _add_nowlez_ext(session, user_id, tier="chambers")
    assert _run(has_nowlez_access(user_id, session)) is True


def test_has_nowlez_access_true_during_trial(session: Session) -> None:
    user_id = _make_user(session)
    _add_nowlez_ext(
        session, user_id,
        tier=None,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=5),
    )
    assert _run(has_nowlez_access(user_id, session)) is True


def test_has_nowlez_access_false_when_trial_expired_no_tier(session: Session) -> None:
    user_id = _make_user(session)
    _add_nowlez_ext(
        session, user_id,
        tier=None,
        trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert _run(has_nowlez_access(user_id, session)) is False


# ---------- is_paying_nowlez_subscriber ----------------------------------


@pytest.mark.parametrize(
    "tier,expected",
    [
        ("advocate", True),
        ("counsel", True),
        ("chambers", True),
        ("free", False),
        (None, False),
    ],
)
def test_is_paying_nowlez_subscriber(
    session: Session, tier: str | None, expected: bool,
) -> None:
    user_id = _make_user(session)
    _add_nowlez_ext(session, user_id, tier=tier)
    assert _run(is_paying_nowlez_subscriber(user_id, session)) is expected


def test_is_paying_nowlez_subscriber_false_when_no_row(session: Session) -> None:
    user_id = _make_user(session)
    assert _run(is_paying_nowlez_subscriber(user_id, session)) is False


# ---------- effective_tier -------------------------------------------------


def test_effective_tier_none_when_no_row(session: Session) -> None:
    user_id = _make_user(session)
    assert _run(effective_tier(user_id, session)) is None


def test_effective_tier_returns_chambers_during_trial(session: Session) -> None:
    user_id = _make_user(session)
    _add_nowlez_ext(
        session, user_id,
        tier=None,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    # Per plan 6.3: "chambers" during trial (trial users get Chambers tier).
    assert _run(effective_tier(user_id, session)) == "chambers"


def test_effective_tier_returns_actual_tier_for_paid_user(session: Session) -> None:
    user_id = _make_user(session)
    _add_nowlez_ext(session, user_id, tier="counsel")
    assert _run(effective_tier(user_id, session)) == "counsel"


def test_effective_tier_after_trial_expired_with_no_tier(session: Session) -> None:
    """Trial expired, no tier picked → no effective tier (None)."""
    user_id = _make_user(session)
    _add_nowlez_ext(
        session, user_id,
        tier=None,
        trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert _run(effective_tier(user_id, session)) is None


# ---------- is_user_eligible_for_munshi_billing — all seven states ---------


def test_state_1_munshi_only_is_eligible(session: Session) -> None:
    user_id = _make_user(session)
    _add_munshi_ext(session, user_id)
    # No nowlez row.
    assert _run(is_user_eligible_for_munshi_billing(user_id, session)) is True


def test_state_2_nowlez_trial_no_munshi_is_not_eligible(session: Session) -> None:
    user_id = _make_user(session)
    _add_nowlez_ext(
        session, user_id, tier=None,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=10),
    )
    # No munshi extension.
    assert _run(is_user_eligible_for_munshi_billing(user_id, session)) is False


def test_state_3_nowlez_paid_no_munshi_is_not_eligible(session: Session) -> None:
    user_id = _make_user(session)
    _add_nowlez_ext(session, user_id, tier="counsel")
    # No munshi ext.
    assert _run(is_user_eligible_for_munshi_billing(user_id, session)) is False


def test_state_4_both_active_paid_nowlez_suppresses_munshi(session: Session) -> None:
    user_id = _make_user(session)
    _add_munshi_ext(session, user_id)
    _add_nowlez_ext(session, user_id, tier="chambers")
    # Even though munshi extension exists, the paid Nowlez tier suppresses
    # Munshi billing.
    assert _run(is_user_eligible_for_munshi_billing(user_id, session)) is False


def test_state_5_munshi_during_nowlez_trial_is_not_eligible(session: Session) -> None:
    """During active trial → no Munshi billing yet."""
    user_id = _make_user(session)
    _add_munshi_ext(session, user_id)
    _add_nowlez_ext(
        session, user_id, tier=None,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=5),
    )
    assert _run(is_user_eligible_for_munshi_billing(user_id, session)) is False


def test_state_6_nowlez_trial_lapsed_munshi_resumes(session: Session) -> None:
    """Trial expired, tier still NULL, munshi ext exists → Munshi billing."""
    user_id = _make_user(session)
    _add_munshi_ext(session, user_id)
    _add_nowlez_ext(
        session, user_id, tier=None,
        trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert _run(is_user_eligible_for_munshi_billing(user_id, session)) is True


def test_state_7_suspended_munshi_is_not_eligible(session: Session) -> None:
    user_id = _make_user(session)
    _add_munshi_ext(session, user_id)
    # Most recent invoice was suspended.
    _add_munshi_invoice_status(
        session, user_id,
        status="suspended",
        cycle_start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        cycle_end=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    assert _run(is_user_eligible_for_munshi_billing(user_id, session)) is False


def test_inactive_user_is_never_eligible(session: Session) -> None:
    user_id = _make_user(session, is_active=False)
    _add_munshi_ext(session, user_id)
    assert _run(is_user_eligible_for_munshi_billing(user_id, session)) is False


def test_state_7_paid_invoice_does_not_block_billing(session: Session) -> None:
    """A paid last invoice should NOT block the next cycle's invoice."""
    user_id = _make_user(session)
    _add_munshi_ext(session, user_id)
    _add_munshi_invoice_status(
        session, user_id,
        status="paid",
        cycle_start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        cycle_end=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    assert _run(is_user_eligible_for_munshi_billing(user_id, session)) is True


def test_only_latest_invoice_status_matters(session: Session) -> None:
    """A historical suspended invoice followed by a paid one is fine."""
    user_id = _make_user(session)
    _add_munshi_ext(session, user_id)
    _add_munshi_invoice_status(
        session, user_id, status="suspended",
        cycle_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        cycle_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    _add_munshi_invoice_status(
        session, user_id, status="paid",
        cycle_start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        cycle_end=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    assert _run(is_user_eligible_for_munshi_billing(user_id, session)) is True
