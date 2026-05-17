"""Munshi grace + suspension state machine tests (Task 6.5.3).

Walks the state machine end-to-end:

    sent → D-5 reminder (status remains 'sent')
    sent → D-0 reminder (status='in_grace')
    in_grace → 7 days elapse → suspend_user → status='suspended'
                                              cases.refresh_enabled=False
    suspended → payment received → resume_user → cases restored
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.munshi.suspension import (
    resume_user,
    send_payment_reminder,
    suspend_user,
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


def _make_user_with_invoice(
    session: Session, *, status: str = "sent", with_munshi_ext: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (user_id, invoice_id) for a user with an active invoice."""
    from data_access.models.billing import MunshiInvoice
    from data_access.models.user import User, UserMunshi

    user = User(phone=f"+91999{uuid.uuid4().hex[:7]}")
    session.add(user)
    session.flush()
    if with_munshi_ext:
        session.add(
            UserMunshi(user_id=user.id, billing_anniversary_date=None)
        )
        session.flush()

    invoice = MunshiInvoice(
        user_id=user.id,
        razorpay_invoice_id=f"inv_{uuid.uuid4().hex[:8]}",
        cycle_start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        cycle_end=datetime(2026, 4, 1, tzinfo=timezone.utc),
        case_count=5,
        amount_paise=5_000,
        status=status,
        due_at=datetime(2026, 4, 8, tzinfo=timezone.utc),
    )
    session.add(invoice)
    session.flush()
    return user.id, invoice.id


def _open_active_case(session: Session, user_id: uuid.UUID) -> uuid.UUID:
    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.case import Case

    c = Case(
        user_id=user_id, cnr=uuid.uuid4().hex[:16], portal="district",
        refresh_enabled=True,
    )
    session.add(c)
    session.flush()
    session.add(
        CaseBillingPeriod(
            user_id=user_id, case_id=c.id,
            period_start=datetime.now(timezone.utc), period_end=None,
        )
    )
    session.flush()
    return c.id


# --- send_payment_reminder -------------------------------------------------


def test_reminder_d5_keeps_status_sent(session: Session) -> None:
    user_id, invoice_id = _make_user_with_invoice(session, status="sent")
    sender = AsyncMock()

    _run(
        send_payment_reminder(
            invoice_id=invoice_id,
            days_offset=-5,  # D-5: due in 5 days.
            send_template_fn=sender,
            session=session,
        )
    )
    session.flush()

    from data_access.models.billing import MunshiInvoice
    invoice = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.id == invoice_id)
    ).scalar_one()
    assert invoice.status == "sent"
    sender.assert_called_once()
    # Template name reflects the day offset.
    assert sender.call_args.kwargs["template"] == "munshi_payment_reminder_d5_v1"


def test_reminder_d0_transitions_to_in_grace(session: Session) -> None:
    user_id, invoice_id = _make_user_with_invoice(session, status="sent")
    sender = AsyncMock()

    _run(
        send_payment_reminder(
            invoice_id=invoice_id,
            days_offset=0,
            send_template_fn=sender,
            session=session,
        )
    )
    session.flush()

    from data_access.models.billing import MunshiInvoice
    invoice = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.id == invoice_id)
    ).scalar_one()
    assert invoice.status == "in_grace"
    assert invoice.grace_expires_at is not None
    assert sender.call_args.kwargs["template"] == "munshi_payment_reminder_d0_v1"


# --- suspend_user ---------------------------------------------------------


def test_suspend_user_disables_refresh_and_closes_periods(
    session: Session,
) -> None:
    user_id, invoice_id = _make_user_with_invoice(session, status="in_grace")
    case_id_1 = _open_active_case(session, user_id)
    case_id_2 = _open_active_case(session, user_id)
    sender = AsyncMock()

    _run(suspend_user(user_id, session, send_template_fn=sender))
    session.flush()

    from data_access.models.billing import CaseBillingPeriod, MunshiInvoice
    from data_access.models.case import Case

    # Invoice marked suspended.
    invoice = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.id == invoice_id)
    ).scalar_one()
    assert invoice.status == "suspended"
    assert invoice.suspended_at is not None

    # All cases paused.
    cases = session.execute(
        select(Case).where(Case.user_id == user_id)
    ).scalars().all()
    assert all(c.refresh_enabled is False for c in cases)

    # All active billing periods closed.
    periods = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.user_id == user_id)
    ).scalars().all()
    assert all(p.period_end is not None for p in periods)

    # Template sent.
    sender.assert_called_once()
    assert sender.call_args.kwargs["template"] == "munshi_suspension_activated_v1"


# --- resume_user ----------------------------------------------------------


def test_resume_user_restores_cases_and_reopens_periods(
    session: Session,
) -> None:
    user_id, invoice_id = _make_user_with_invoice(session, status="suspended")
    case_id_1 = _open_active_case(session, user_id)
    case_id_2 = _open_active_case(session, user_id)
    # Simulate suspension: close periods + flip refresh_enabled.
    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.case import Case

    now = datetime.now(timezone.utc)
    session.execute(
        CaseBillingPeriod.__table__.update()
        .where(CaseBillingPeriod.user_id == user_id)
        .where(CaseBillingPeriod.period_end.is_(None))
        .values(period_end=now)
    )
    session.execute(
        Case.__table__.update()
        .where(Case.user_id == user_id)
        .values(refresh_enabled=False)
    )
    session.flush()

    # Stash which cases were active before suspension on users_munshi.current_state
    # so resume can restore them. (See suspension docstring for rationale.)
    from data_access.models.user import UserMunshi
    um = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one()
    um.current_state = {
        "was_active_before_suspension": [str(case_id_1), str(case_id_2)],
    }
    session.flush()

    _run(resume_user(user_id, session))
    session.flush()

    cases = session.execute(
        select(Case).where(Case.user_id == user_id)
    ).scalars().all()
    assert all(c.refresh_enabled is True for c in cases)

    # New billing periods opened.
    open_periods = session.execute(
        select(CaseBillingPeriod)
        .where(CaseBillingPeriod.user_id == user_id)
        .where(CaseBillingPeriod.period_end.is_(None))
    ).scalars().all()
    assert len(open_periods) == 2


def test_suspend_then_resume_round_trip(session: Session) -> None:
    user_id, invoice_id = _make_user_with_invoice(session, status="in_grace")
    _open_active_case(session, user_id)
    sender = AsyncMock()

    _run(suspend_user(user_id, session, send_template_fn=sender))
    session.flush()
    _run(resume_user(user_id, session))
    session.flush()

    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.case import Case

    cases = session.execute(
        select(Case).where(Case.user_id == user_id)
    ).scalars().all()
    assert all(c.refresh_enabled is True for c in cases)

    open_periods = session.execute(
        select(CaseBillingPeriod)
        .where(CaseBillingPeriod.user_id == user_id)
        .where(CaseBillingPeriod.period_end.is_(None))
    ).scalars().all()
    assert len(open_periods) >= 1
