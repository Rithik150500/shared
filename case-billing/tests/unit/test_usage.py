"""Case-billing period DAO unit tests (Task 6.2.2).

Exercises `open_billing_period`, `close_billing_period`, and
`count_billable_cases_in_window` against an in-memory SQLite copy of the
billing schema.

These tests reuse the sync `Session` fixture style from `test_pricing.py`
because the underlying SQLAlchemy engine is sync; the DAO entry points
are `async def` to match the public API of the case-billing package, but
internally they delegate to `Session.execute` (no background work).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.munshi.usage import (
    close_billing_period,
    count_billable_cases_in_window,
    open_billing_period,
)


@pytest.fixture()
def session() -> Session:
    """In-memory SQLite session with the full billing schema."""
    from data_access.base import Base

    # Importing these registers the User / Case / CaseBillingPeriod mappers
    # on Base.metadata so create_all() builds every dependent table.
    import data_access.models.auth  # noqa: F401
    import data_access.models.case  # noqa: F401
    import data_access.models.billing  # noqa: F401
    import data_access.models.user  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _make_user(session: Session) -> uuid.UUID:
    from data_access.models.user import User

    user = User(phone=f"+91999{uuid.uuid4().hex[:7]}")
    session.add(user)
    session.flush()
    return user.id


def _make_case(session: Session, user_id: uuid.UUID) -> uuid.UUID:
    from data_access.models.case import Case

    case = Case(
        user_id=user_id,
        cnr=uuid.uuid4().hex[:16],
        portal="district",
    )
    session.add(case)
    session.flush()
    return case.id


def _run(coro):
    """Run an async coroutine to completion for sync tests."""
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# --- open_billing_period: idempotency --------------------------------------


def test_open_billing_period_inserts_active_row(session: Session) -> None:
    user_id = _make_user(session)
    case_id = _make_case(session, user_id)
    now = datetime.now(timezone.utc)

    _run(open_billing_period(user_id, case_id, now, session))
    session.flush()

    from data_access.models.billing import CaseBillingPeriod
    rows = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.case_id == case_id)
    ).scalars().all()

    assert len(rows) == 1
    assert rows[0].period_end is None
    # CaseBillingPeriod uses plain `with_variant(String(36), "sqlite")` so
    # the round-trip yields a str on SQLite. We compare on the string form.
    assert str(rows[0].user_id) == str(user_id)


def test_open_billing_period_is_idempotent_when_active_row_exists(
    session: Session,
) -> None:
    user_id = _make_user(session)
    case_id = _make_case(session, user_id)
    now = datetime.now(timezone.utc)

    _run(open_billing_period(user_id, case_id, now, session))
    _run(open_billing_period(user_id, case_id, now + timedelta(seconds=5), session))
    session.flush()

    from data_access.models.billing import CaseBillingPeriod
    rows = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.case_id == case_id)
    ).scalars().all()

    # Only one row: the second open is a no-op because period_end IS NULL
    # on the first row.
    assert len(rows) == 1


# --- close_billing_period --------------------------------------------------


def test_close_billing_period_sets_period_end(session: Session) -> None:
    user_id = _make_user(session)
    case_id = _make_case(session, user_id)
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=10)

    _run(open_billing_period(user_id, case_id, start, session))
    _run(close_billing_period(user_id, case_id, end, session))
    session.flush()

    from data_access.models.billing import CaseBillingPeriod
    row = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.case_id == case_id)
    ).scalar_one()

    assert row.period_end is not None


def test_close_billing_period_noop_if_no_active_row(session: Session) -> None:
    user_id = _make_user(session)
    case_id = _make_case(session, user_id)
    now = datetime.now(timezone.utc)
    # Close without ever opening — should not raise, should not insert anything.
    _run(close_billing_period(user_id, case_id, now, session))
    session.flush()

    from data_access.models.billing import CaseBillingPeriod
    rows = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.case_id == case_id)
    ).scalars().all()
    assert rows == []


# --- count_billable_cases_in_window ---------------------------------------


def test_count_billable_open_then_closed_within_cycle(session: Session) -> None:
    user_id = _make_user(session)
    case_id = _make_case(session, user_id)
    cycle_start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    cycle_end = datetime(2026, 4, 1, tzinfo=timezone.utc)

    open_time = cycle_start + timedelta(days=2)
    close_time = cycle_start + timedelta(days=10)
    _run(open_billing_period(user_id, case_id, open_time, session))
    _run(close_billing_period(user_id, case_id, close_time, session))
    session.flush()

    count = _run(
        count_billable_cases_in_window(user_id, cycle_start, cycle_end, session)
    )
    assert count == 1


def test_count_billable_open_at_cycle_end_no_close(session: Session) -> None:
    """A case still open at the end of the cycle counts."""
    user_id = _make_user(session)
    case_id = _make_case(session, user_id)
    cycle_start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    cycle_end = datetime(2026, 4, 1, tzinfo=timezone.utc)

    _run(open_billing_period(
        user_id, case_id, cycle_start + timedelta(days=5), session,
    ))
    session.flush()

    count = _run(
        count_billable_cases_in_window(user_id, cycle_start, cycle_end, session)
    )
    assert count == 1


def test_count_billable_case_entirely_before_cycle(session: Session) -> None:
    user_id = _make_user(session)
    case_id = _make_case(session, user_id)
    cycle_start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    cycle_end = datetime(2026, 4, 1, tzinfo=timezone.utc)

    _run(open_billing_period(
        user_id, case_id, datetime(2026, 1, 1, tzinfo=timezone.utc), session,
    ))
    _run(close_billing_period(
        user_id, case_id, datetime(2026, 1, 20, tzinfo=timezone.utc), session,
    ))
    session.flush()

    count = _run(
        count_billable_cases_in_window(user_id, cycle_start, cycle_end, session)
    )
    assert count == 0


def test_count_billable_distinct_case_id_dedup(session: Session) -> None:
    """Case opened, closed, reopened in same cycle → DISTINCT counts 1."""
    user_id = _make_user(session)
    case_id = _make_case(session, user_id)
    cycle_start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    cycle_end = datetime(2026, 4, 1, tzinfo=timezone.utc)

    _run(open_billing_period(
        user_id, case_id, cycle_start + timedelta(days=1), session,
    ))
    _run(close_billing_period(
        user_id, case_id, cycle_start + timedelta(days=5), session,
    ))
    _run(open_billing_period(
        user_id, case_id, cycle_start + timedelta(days=10), session,
    ))
    _run(close_billing_period(
        user_id, case_id, cycle_start + timedelta(days=15), session,
    ))
    session.flush()

    count = _run(
        count_billable_cases_in_window(user_id, cycle_start, cycle_end, session)
    )
    assert count == 1


def test_count_billable_two_distinct_cases(session: Session) -> None:
    user_id = _make_user(session)
    c1 = _make_case(session, user_id)
    c2 = _make_case(session, user_id)
    cycle_start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    cycle_end = datetime(2026, 4, 1, tzinfo=timezone.utc)

    _run(open_billing_period(user_id, c1, cycle_start + timedelta(days=2), session))
    _run(open_billing_period(user_id, c2, cycle_start + timedelta(days=3), session))
    session.flush()

    count = _run(
        count_billable_cases_in_window(user_id, cycle_start, cycle_end, session)
    )
    assert count == 2


def test_count_billable_returns_raw_count_not_capped(session: Session) -> None:
    """200-cap is enforced at invoice generation, not by the counter."""
    user_id = _make_user(session)
    cycle_start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    cycle_end = datetime(2026, 4, 1, tzinfo=timezone.utc)

    # Create 250 cases all open during the cycle.
    for _ in range(250):
        cid = _make_case(session, user_id)
        _run(open_billing_period(
            user_id, cid, cycle_start + timedelta(days=1), session,
        ))
    session.flush()

    count = _run(
        count_billable_cases_in_window(user_id, cycle_start, cycle_end, session)
    )
    assert count == 250
