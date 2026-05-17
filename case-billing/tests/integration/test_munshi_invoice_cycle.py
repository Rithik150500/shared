"""Integration tests for Munshi anniversary invoice generation (Task 6.4.4).

Uses the in-memory SQLite session pattern and a fake Razorpay client to
exercise the full anniversary-cycle flow end-to-end without hitting the
network:

  case_billing_periods (DAO) → eligibility check → cycle window
    → DISTINCT case count → 200-cap clamp → invoice amount in paise
    → Razorpay customer + invoice + payment links + UPI link
    → MunshiInvoice row insert → WhatsApp template send

The Razorpay client functions are stubbed via a small ``FakeRazorpayClient``
that records calls; the template send is captured into a list.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.munshi.invoices import generate_anniversary_invoice


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


# ---------- helpers --------------------------------------------------------


def _make_munshi_user(
    session: Session,
    *,
    anniversary: date = date(2026, 3, 15),
    phone: str | None = None,
) -> uuid.UUID:
    from data_access.models.user import User, UserMunshi

    user = User(
        phone=phone or f"+91999{uuid.uuid4().hex[:7]}",
        email=f"u{uuid.uuid4().hex[:8]}@example.com",
    )
    session.add(user)
    session.flush()
    session.add(UserMunshi(user_id=user.id, billing_anniversary_date=anniversary))
    session.flush()
    return user.id


def _open_case_periods(
    session: Session,
    user_id: uuid.UUID,
    n: int,
    period_start: datetime,
) -> list[uuid.UUID]:
    """Insert `n` cases, each with an active billing period since `period_start`."""
    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.case import Case

    case_ids: list[uuid.UUID] = []
    for _ in range(n):
        c = Case(
            user_id=user_id,
            cnr=uuid.uuid4().hex[:16],
            portal="district",
        )
        session.add(c)
        session.flush()
        session.add(
            CaseBillingPeriod(
                user_id=user_id, case_id=c.id,
                period_start=period_start, period_end=None,
            )
        )
        case_ids.append(c.id)
    session.flush()
    return case_ids


class FakeRazorpayClient:
    """Minimal stand-in for the case_billing razorpay HTTP client.

    Records every call so individual tests can assert on payloads without
    wiring respx for every endpoint. The real package routes through the
    submodule functions ``customers.create_customer`` etc.; here we just
    pre-bake the returned IDs and short URLs.
    """

    def __init__(self) -> None:
        self.customer_calls: list[dict[str, Any]] = []
        self.invoice_calls: list[dict[str, Any]] = []
        self.payment_link_calls: list[dict[str, Any]] = []
        self.upi_link_calls: list[dict[str, Any]] = []
        self.next_customer_id = "cust_test_123"
        self.next_invoice_id = "inv_test_abc"
        self.next_payment_link_short_url = "https://rzp.io/i/pl_test"
        self.next_upi_link_short_url = "https://rzp.io/i/upi_test"


async def _fake_create_customer(client: FakeRazorpayClient, **kwargs: Any) -> dict[str, Any]:
    client.customer_calls.append(kwargs)
    return {"id": client.next_customer_id, **kwargs}


async def _fake_create_invoice(client: FakeRazorpayClient, **kwargs: Any) -> dict[str, Any]:
    client.invoice_calls.append(kwargs)
    return {
        "id": client.next_invoice_id,
        "short_url": "https://rzp.io/i/inv_test",
        "status": "issued",
        **kwargs,
    }


async def _fake_create_payment_link(client: FakeRazorpayClient, **kwargs: Any) -> dict[str, Any]:
    client.payment_link_calls.append(kwargs)
    return {
        "id": "pl_test",
        "short_url": client.next_payment_link_short_url,
        **kwargs,
    }


async def _fake_create_upi_link(client: FakeRazorpayClient, **kwargs: Any) -> dict[str, Any]:
    client.upi_link_calls.append(kwargs)
    return {
        "id": "pl_test_upi",
        "short_url": client.next_upi_link_short_url,
        **kwargs,
    }


def _patch_razorpay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the Razorpay helpers the invoice generator imports."""
    from case_billing.munshi import invoices as inv_mod

    monkeypatch.setattr(inv_mod, "rzp_create_customer", _fake_create_customer)
    monkeypatch.setattr(inv_mod, "rzp_create_invoice", _fake_create_invoice)
    monkeypatch.setattr(inv_mod, "rzp_create_payment_link", _fake_create_payment_link)
    monkeypatch.setattr(inv_mod, "rzp_create_upi_link", _fake_create_upi_link)


def _make_template_sender() -> AsyncMock:
    """Return an AsyncMock that satisfies the `send_template_fn` contract."""
    return AsyncMock(return_value="wamid_test")


# ---------- happy path -----------------------------------------------------


def test_happy_path_creates_invoice_and_sends_template(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_razorpay(monkeypatch)
    today = date(2026, 4, 15)
    # Anniversary on the 15th — today is anniversary so cycle is the
    # preceding month.
    user_id = _make_munshi_user(session, anniversary=date(2026, 3, 15))
    # Five cases active for the whole cycle.
    _open_case_periods(
        session, user_id, n=5,
        period_start=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )

    client = FakeRazorpayClient()
    sender = _make_template_sender()

    invoice = _run(
        generate_anniversary_invoice(
            user_id=user_id,
            session=session,
            razorpay_client=client,
            send_template_fn=sender,
            today=today,
        )
    )
    session.flush()

    assert invoice is not None
    assert invoice.case_count == 5
    assert invoice.amount_paise == 5 * 1000  # 5 cases × ₹10
    assert invoice.status == "sent"
    assert invoice.razorpay_invoice_id == client.next_invoice_id

    # Razorpay was called for customer + invoice + payment link + UPI link.
    assert len(client.customer_calls) == 1
    assert len(client.invoice_calls) == 1
    assert len(client.payment_link_calls) == 1
    assert len(client.upi_link_calls) == 1

    # WhatsApp template was sent exactly once.
    sender.assert_called_once()
    call_kwargs = sender.call_args.kwargs
    assert call_kwargs["template"] == "munshi_invoice_payment_v1"
    assert call_kwargs["brand"] == "munshi"
    assert "case_count" in call_kwargs["variables"]
    assert call_kwargs["variables"]["case_count"] == 5
    assert call_kwargs["variables"]["amount_rupees"] == 50


def test_200_case_cap_clamps_invoice_amount(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_razorpay(monkeypatch)
    user_id = _make_munshi_user(session, anniversary=date(2026, 3, 15))
    _open_case_periods(
        session, user_id, n=250,
        period_start=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )

    invoice = _run(
        generate_anniversary_invoice(
            user_id=user_id,
            session=session,
            razorpay_client=FakeRazorpayClient(),
            send_template_fn=_make_template_sender(),
            today=date(2026, 4, 15),
        )
    )
    session.flush()

    assert invoice is not None
    # Raw count preserved, but billed amount uses min(count, 200).
    assert invoice.case_count == 250
    assert invoice.amount_paise == 200 * 1000


def test_zero_cases_skips_invoice(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_razorpay(monkeypatch)
    user_id = _make_munshi_user(session, anniversary=date(2026, 3, 15))
    # No cases opened.

    sender = _make_template_sender()
    invoice = _run(
        generate_anniversary_invoice(
            user_id=user_id,
            session=session,
            razorpay_client=FakeRazorpayClient(),
            send_template_fn=sender,
            today=date(2026, 4, 15),
        )
    )
    session.flush()

    assert invoice is None
    sender.assert_not_called()

    from data_access.models.billing import MunshiInvoice
    rows = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.user_id == user_id)
    ).scalars().all()
    assert rows == []


def test_ineligible_user_returns_none(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User with Nowlez 'counsel' tier → no Munshi invoice."""
    _patch_razorpay(monkeypatch)
    user_id = _make_munshi_user(session, anniversary=date(2026, 3, 15))
    _open_case_periods(
        session, user_id, n=10,
        period_start=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    from data_access.models.user import UserNowlez
    session.add(UserNowlez(user_id=user_id, name="x", tier="counsel"))
    session.flush()

    invoice = _run(
        generate_anniversary_invoice(
            user_id=user_id,
            session=session,
            razorpay_client=FakeRazorpayClient(),
            send_template_fn=_make_template_sender(),
            today=date(2026, 4, 15),
        )
    )
    assert invoice is None


def test_idempotent_within_same_cycle(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running twice on the same cycle returns the existing invoice once."""
    _patch_razorpay(monkeypatch)
    user_id = _make_munshi_user(session, anniversary=date(2026, 3, 15))
    _open_case_periods(
        session, user_id, n=3,
        period_start=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )

    first = _run(
        generate_anniversary_invoice(
            user_id=user_id,
            session=session,
            razorpay_client=FakeRazorpayClient(),
            send_template_fn=_make_template_sender(),
            today=date(2026, 4, 15),
        )
    )
    second = _run(
        generate_anniversary_invoice(
            user_id=user_id,
            session=session,
            razorpay_client=FakeRazorpayClient(),
            send_template_fn=_make_template_sender(),
            today=date(2026, 4, 15),
        )
    )
    session.flush()

    assert first is not None
    # Second call returns None (already invoiced this cycle).
    assert second is None

    from data_access.models.billing import MunshiInvoice
    rows = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.user_id == user_id)
    ).scalars().all()
    assert len(rows) == 1
