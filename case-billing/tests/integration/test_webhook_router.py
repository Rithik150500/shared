"""Integration tests for the unified Razorpay webhook router (Task 8.2.2).

Covers signature verification, replay protection, and per-event dispatch
against in-memory SQLite. Each test builds a realistic Razorpay webhook
body, HMAC-signs it with a test secret, and asserts both the
:class:`WebhookResult` and the resulting DB state.

The Razorpay sample event JSONs are constructed inline (rather than
loaded from fixture files) because the test secret needs to recompute
the HMAC every run — pre-baked signatures would not match the freshly
generated bodies.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.errors import WebhookSignatureInvalid
from case_billing.shared.webhook_router import (
    STATUS_ALREADY_PROCESSED,
    STATUS_DEFERRED,
    STATUS_PROCESSED,
    STATUS_UNHANDLED_EVENT,
    handle_unified_webhook,
)


WEBHOOK_SECRET = "test_secret_do_not_use_in_prod"


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


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Compute Razorpay's HMAC-SHA256 hex digest for the test body."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _wrap_event(
    event_type: str,
    *,
    event_id: str | None = None,
    subscription: dict[str, Any] | None = None,
    invoice: dict[str, Any] | None = None,
) -> tuple[bytes, str]:
    """Build a (raw_body, signature) tuple for a synthetic Razorpay event."""
    body: dict[str, Any] = {
        "entity": "event",
        "account_id": "acc_test",
        "event": event_type,
        "contains": [],
        "payload": {},
        "created_at": 1714600000,
        "id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
    }
    if subscription is not None:
        body["payload"]["subscription"] = {"entity": subscription}
        body["contains"].append("subscription")
    if invoice is not None:
        body["payload"]["invoice"] = {"entity": invoice}
        body["contains"].append("invoice")
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return raw, _sign(raw)


def _fake_razorpay_client() -> Any:
    """Minimal stand-in — no methods needed for handlers that DO call it,
    they reach through monkeypatched module-level rzp_* aliases."""
    class _Stub:
        pass
    return _Stub()


def _make_subscription_with_user(
    session: Session,
    *,
    rzp_sub_id: str = "sub_test_active",
    status: str = "trialing",
    tier: str = "chambers",
    intro_promo_state: str = "pre_first_payment",
    referral_state: str = "no_referral",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create User + UserNowlez + Subscription. Returns (user_id, sub_id)."""
    from data_access.models.billing import Subscription
    from data_access.models.user import User, UserNowlez

    user = User(phone=f"+91999{uuid.uuid4().hex[:7]}")
    session.add(user)
    session.flush()
    session.add(UserNowlez(user_id=user.id, name="Test", tier=tier))
    session.flush()
    sub = Subscription(
        user_id=user.id,
        tier=tier,
        billing_cycle="monthly",
        razorpay_subscription_id=rzp_sub_id,
        status=status,
        intro_promo_state=intro_promo_state,
        referral_state=referral_state,
    )
    session.add(sub)
    session.flush()
    return user.id, sub.id


def _make_munshi_invoice(
    session: Session,
    *,
    user_id: uuid.UUID,
    razorpay_invoice_id: str,
    status: str = "sent",
) -> uuid.UUID:
    from data_access.models.billing import MunshiInvoice
    from data_access.models.user import UserMunshi

    existing = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one_or_none()
    if existing is None:
        session.add(UserMunshi(user_id=user_id))
        session.flush()

    inv = MunshiInvoice(
        user_id=user_id,
        razorpay_invoice_id=razorpay_invoice_id,
        cycle_start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        cycle_end=datetime(2026, 4, 1, tzinfo=timezone.utc),
        case_count=5,
        amount_paise=5000,
        status=status,
        due_at=datetime(2026, 4, 8, tzinfo=timezone.utc),
    )
    session.add(inv)
    session.flush()
    return inv.id


# ---------- signature verification ------------------------------------------


def test_invalid_signature_raises_and_writes_no_rows(session: Session) -> None:
    from data_access.models.billing import PaymentEvent

    raw, _good_sig = _wrap_event(
        "subscription.activated",
        subscription={"id": "sub_nope"},
    )
    bad_sig = "00" * 32  # 64-char hex but wrong

    with pytest.raises(WebhookSignatureInvalid):
        _run(
            handle_unified_webhook(
                raw_body=raw,
                signature=bad_sig,
                secret=WEBHOOK_SECRET,
                session=session,
                razorpay_client=_fake_razorpay_client(),
                send_template_fn=AsyncMock(),
            )
        )

    # No payment_events row should be persisted.
    rows = session.execute(select(PaymentEvent)).scalars().all()
    assert rows == []


def test_missing_signature_raises(session: Session) -> None:
    raw, _ = _wrap_event(
        "subscription.activated", subscription={"id": "sub_nope"},
    )
    with pytest.raises(WebhookSignatureInvalid):
        _run(
            handle_unified_webhook(
                raw_body=raw,
                signature=None,
                secret=WEBHOOK_SECRET,
                session=session,
                razorpay_client=_fake_razorpay_client(),
                send_template_fn=AsyncMock(),
            )
        )


# ---------- idempotency / replay --------------------------------------------


def test_replay_same_event_id_returns_already_processed(
    session: Session,
) -> None:
    """A repeat of the same event id no-ops the handler and reports
    ``already_processed``. Only one ``payment_events`` row exists."""
    from data_access.models.billing import PaymentEvent

    user_id, sub_id = _make_subscription_with_user(
        session, rzp_sub_id="sub_replay",
    )
    sender = AsyncMock()

    raw, sig = _wrap_event(
        "subscription.activated",
        event_id="evt_replay_xyz",
        subscription={
            "id": "sub_replay",
            "current_start": 1714600000,
            "current_end": 1717278400,
            "notes": {"product": "nowlez"},
        },
    )

    first = _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=sender,
        )
    )
    session.flush()

    second = _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=sender,
        )
    )
    session.flush()

    assert first.status == STATUS_PROCESSED
    assert second.status == STATUS_ALREADY_PROCESSED

    # Only one payment_events row.
    rows = session.execute(
        select(PaymentEvent).where(
            PaymentEvent.razorpay_event_id == "evt_replay_xyz"
        )
    ).scalars().all()
    assert len(rows) == 1

    # And the send_template_fn was invoked only once (first call only).
    assert sender.call_count == 1


# ---------- subscription.activated ------------------------------------------


def test_subscription_activated_flips_status_and_sends_welcome(
    session: Session,
) -> None:
    user_id, sub_id = _make_subscription_with_user(
        session,
        rzp_sub_id="sub_act_001",
        status="trialing",
        intro_promo_state="pre_first_payment",
    )
    sender = AsyncMock()

    raw, sig = _wrap_event(
        "subscription.activated",
        subscription={
            "id": "sub_act_001",
            "status": "active",
            "current_start": 1714600000,
            "current_end": 1717278400,
            "notes": {"product": "nowlez", "user_id": str(user_id)},
        },
    )
    result = _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=sender,
        )
    )
    session.flush()

    assert result.status == STATUS_PROCESSED
    assert result.event_type == "subscription.activated"
    assert result.product == "nowlez"

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub_id)
    ).scalar_one()
    assert sub.status == "active"
    assert sub.intro_promo_state == "in_intro"
    assert sub.period_start is not None
    assert sub.period_end is not None

    # Welcome template fired.
    sender.assert_called_once()
    assert sender.call_args.kwargs["template"] == "nowlez_subscription_started_v1"
    assert sender.call_args.kwargs["brand"] == "nowlez"


def test_subscription_activated_voids_pending_munshi_invoices(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-cycle upgrade: when a user activates Nowlez, any pending
    Munshi invoices for them must be voided so they aren't double-billed."""
    # Stub the razorpay void call — the void_invoice helper goes through
    # case_billing.munshi.invoices.rzp_void_invoice.
    from case_billing.munshi import invoices as inv_mod
    monkeypatch.setattr(inv_mod, "rzp_void_invoice", AsyncMock(return_value=None))

    user_id, sub_id = _make_subscription_with_user(
        session, rzp_sub_id="sub_act_void",
    )
    inv_id = _make_munshi_invoice(
        session, user_id=user_id,
        razorpay_invoice_id="inv_to_void", status="sent",
    )

    sender = AsyncMock()
    raw, sig = _wrap_event(
        "subscription.activated",
        subscription={
            "id": "sub_act_void",
            "current_start": 1714600000,
            "current_end": 1717278400,
            "notes": {"product": "nowlez"},
        },
    )
    result = _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=sender,
        )
    )
    session.flush()

    assert result.status == STATUS_PROCESSED
    assert str(inv_id) in result.details["munshi_invoices_voided"]

    from data_access.models.billing import MunshiInvoice
    voided = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.id == inv_id)
    ).scalar_one()
    assert voided.status == "voided"


def test_subscription_activated_closes_open_billing_periods(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from case_billing.munshi import invoices as inv_mod
    monkeypatch.setattr(inv_mod, "rzp_void_invoice", AsyncMock(return_value=None))

    user_id, sub_id = _make_subscription_with_user(
        session, rzp_sub_id="sub_act_close",
    )

    # Add an open billing period to be closed.
    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.case import Case
    c = Case(user_id=user_id, cnr=uuid.uuid4().hex[:16], portal="district")
    session.add(c)
    session.flush()
    session.add(
        CaseBillingPeriod(
            user_id=user_id, case_id=c.id,
            period_start=datetime.now(timezone.utc) - timedelta(days=10),
            period_end=None,
        )
    )
    session.flush()

    raw, sig = _wrap_event(
        "subscription.activated",
        subscription={
            "id": "sub_act_close",
            "current_start": 1714600000,
            "current_end": 1717278400,
            "notes": {"product": "nowlez"},
        },
    )
    _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=AsyncMock(),
        )
    )
    session.flush()

    periods = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.user_id == user_id)
    ).scalars().all()
    assert all(p.period_end is not None for p in periods)


# ---------- subscription.charged --------------------------------------------


def test_subscription_charged_terminal_intro_and_renewal_template(
    session: Session,
) -> None:
    user_id, sub_id = _make_subscription_with_user(
        session,
        rzp_sub_id="sub_chrg_001",
        status="active",
        intro_promo_state="in_intro",
    )
    sender = AsyncMock()

    raw, sig = _wrap_event(
        "subscription.charged",
        subscription={
            "id": "sub_chrg_001",
            "notes": {"product": "nowlez"},
        },
    )
    result = _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=sender,
        )
    )
    session.flush()

    assert result.status == STATUS_PROCESSED

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub_id)
    ).scalar_one()
    assert sub.intro_promo_state == "past_intro"

    sender.assert_called_once()
    assert sender.call_args.kwargs["template"] == "nowlez_renewal_success_v1"


# ---------- subscription.cancelled ------------------------------------------


def test_subscription_cancelled_flips_status(
    session: Session,
) -> None:
    user_id, sub_id = _make_subscription_with_user(
        session, rzp_sub_id="sub_cancel_001", status="active",
    )

    raw, sig = _wrap_event(
        "subscription.cancelled",
        subscription={"id": "sub_cancel_001"},
    )
    result = _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=AsyncMock(),
        )
    )
    session.flush()

    assert result.status == STATUS_PROCESSED

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub_id)
    ).scalar_one()
    assert sub.status == "cancelled"


# ---------- invoice.paid -----------------------------------------------------


def test_invoice_paid_munshi_marks_invoice_paid(session: Session) -> None:
    """An ``invoice.paid`` event with ``notes.product='munshi'`` calls
    ``mark_invoice_paid`` which flips status='paid'."""
    user_id, sub_id = _make_subscription_with_user(
        session, rzp_sub_id="sub_unused_paid",
    )
    inv_id = _make_munshi_invoice(
        session, user_id=user_id,
        razorpay_invoice_id="inv_paid_001", status="sent",
    )

    sender = AsyncMock()
    raw, sig = _wrap_event(
        "invoice.paid",
        invoice={
            "id": "inv_paid_001",
            "notes": {"product": "munshi"},
        },
    )
    result = _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=sender,
        )
    )
    session.flush()

    assert result.status == STATUS_PROCESSED
    assert result.product == "munshi"
    assert result.details["munshi_invoice_id"] == str(inv_id)

    from data_access.models.billing import MunshiInvoice
    inv = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.id == inv_id)
    ).scalar_one()
    assert inv.status == "paid"
    assert inv.paid_at is not None

    sender.assert_called_once()
    assert sender.call_args.kwargs["template"] == "munshi_payment_received_v1"


def test_invoice_paid_nowlez_is_noop(session: Session) -> None:
    """Nowlez ``invoice.paid`` is a no-op — the canonical signal is
    ``subscription.charged``, so dispatching here would double-process."""
    sender = AsyncMock()
    raw, sig = _wrap_event(
        "invoice.paid",
        invoice={
            "id": "inv_nowlez_unused",
            "notes": {"product": "nowlez"},
        },
    )
    result = _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=sender,
        )
    )

    assert result.status == STATUS_PROCESSED
    assert result.details["noop_reason"] == "covered_by_subscription_charged"
    sender.assert_not_called()


# ---------- invoice.expired -------------------------------------------------


def test_invoice_expired_munshi_transitions_to_in_grace(
    session: Session,
) -> None:
    user_id, _ = _make_subscription_with_user(
        session, rzp_sub_id="sub_unused_exp",
    )
    inv_id = _make_munshi_invoice(
        session, user_id=user_id,
        razorpay_invoice_id="inv_exp_001", status="sent",
    )

    raw, sig = _wrap_event(
        "invoice.expired",
        invoice={
            "id": "inv_exp_001",
            "notes": {"product": "munshi"},
        },
    )
    result = _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=AsyncMock(),
        )
    )
    session.flush()

    assert result.status == STATUS_PROCESSED
    assert result.details["new_status"] == "in_grace"

    from data_access.models.billing import MunshiInvoice
    inv = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.id == inv_id)
    ).scalar_one()
    assert inv.status == "in_grace"


# ---------- unhandled / deferred --------------------------------------------


def test_unknown_event_type_returns_unhandled(session: Session) -> None:
    raw, sig = _wrap_event("custom.weird_event")
    result = _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=AsyncMock(),
        )
    )
    session.flush()
    assert result.status == STATUS_UNHANDLED_EVENT

    # Still recorded in payment_events for audit.
    from data_access.models.billing import PaymentEvent
    rows = session.execute(select(PaymentEvent)).scalars().all()
    assert len(rows) == 1


def test_payment_link_event_returns_deferred(session: Session) -> None:
    raw, sig = _wrap_event(
        "payment_link.paid",
        invoice=None,
    )
    result = _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=AsyncMock(),
        )
    )
    session.flush()
    assert result.status == STATUS_DEFERRED


def test_payload_unparseable_returns_unhandled(session: Session) -> None:
    raw = b"\xff not json \x00"
    sig = _sign(raw)
    result = _run(
        handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=_fake_razorpay_client(),
            send_template_fn=AsyncMock(),
        )
    )
    assert result.status == STATUS_UNHANDLED_EVENT
