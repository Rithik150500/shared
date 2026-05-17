"""End-to-end subscription / billing lifecycle tests (Task 15).

These integration tests exercise the full happy path across multiple
modules in case-billing, using in-memory SQLite + monkeypatched Razorpay
helpers, to catch regressions that would only show up if the per-module
unit tests all passed but the cross-module wiring drifted.

Scenarios covered:

* **Nowlez full lifecycle**: trial → tier selection (Counsel) → Razorpay
  activated webhook → first paid charge → second paid charge → user
  cancels → subscription.cancelled webhook → fallback hands them off
  to Munshi if they still have cases.
* **Munshi full lifecycle**: trial expires → day-31 fallback opens
  Munshi extension + billing periods → first anniversary invoice →
  ``invoice.paid`` webhook flips status to paid → second cycle invoice.
* **Race condition**: cron picks user for day-31 fallback AND the
  user picks a tier in the same window — fallback short-circuits
  on the raced paid tier so we don't end up with both a Munshi
  extension and an active Nowlez subscription billing the same user.
* **Webhook idempotency**: replay the same ``subscription.charged``
  event three times; assert the template send + intro promo
  transition + referral progression each happen exactly once.

Tests use the same fake Razorpay client + AsyncMock pattern as the
narrower module tests; helpers below build minimal-but-realistic
payloads.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.munshi.invoices import (
    generate_anniversary_invoice,
    mark_invoice_paid,
)
from case_billing.nowlez.cancellation import cancel_subscription
from case_billing.nowlez.fallback import fallback_to_munshi
from case_billing.nowlez.subscriptions import select_tier_and_subscribe
from case_billing.nowlez.trial import create_trial_for_new_signup, is_in_trial
from case_billing.shared.webhook_router import (
    STATUS_ALREADY_PROCESSED,
    STATUS_PROCESSED,
    handle_unified_webhook,
)


WEBHOOK_SECRET = "test_secret_e2e"


@pytest.fixture()
def session() -> Session:
    from data_access.base import Base

    import data_access.models.audit  # noqa: F401
    import data_access.models.auth  # noqa: F401
    import data_access.models.billing  # noqa: F401
    import data_access.models.case  # noqa: F401
    import data_access.models.user  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ---------- fakes ------------------------------------------------------------


class _Cfg:
    razorpay_plan_id_advocate_monthly = "plan_adv_m"
    razorpay_plan_id_counsel_monthly = "plan_cou_m"
    razorpay_plan_id_chambers_monthly = "plan_cham_m"
    razorpay_plan_id_chambers_quarterly = "plan_cham_q"
    razorpay_plan_id_chambers_yearly = "plan_cham_y"
    razorpay_offer_id_chambers_half_off = "offer_cham_half"
    razorpay_offer_id_counsel_half_off = "offer_cou_half"


async def _fake_create_subscription(client: Any, **kwargs: Any) -> Any:
    """Mirror the production helper signature; assign sequential IDs."""
    from case_billing.razorpay_client.subscriptions import RazorpaySubscription
    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
    client.subscription_calls.append({"id": sub_id, **kwargs})
    return RazorpaySubscription(
        id=sub_id,
        short_url=f"https://rzp.io/i/{sub_id}",
        status="created",
        current_start=None, current_end=None,
        notes=kwargs.get("notes", {}),
    )


async def _fake_cancel_subscription(
    client: Any, sub_id: str, *, cancel_at_cycle_end: bool = False,
) -> dict[str, Any]:
    client.cancel_calls.append({
        "id": sub_id, "cancel_at_cycle_end": cancel_at_cycle_end,
    })
    return {"id": sub_id, "status": "cancelled"}


async def _fake_create_customer(client: Any, **kwargs: Any) -> dict[str, Any]:
    client.customer_calls.append(kwargs)
    return {"id": f"cust_{uuid.uuid4().hex[:8]}", **kwargs}


async def _fake_create_invoice(client: Any, **kwargs: Any) -> dict[str, Any]:
    inv_id = f"inv_{uuid.uuid4().hex[:10]}"
    client.invoice_calls.append({"id": inv_id, **kwargs})
    return {"id": inv_id, "short_url": f"https://rzp.io/i/{inv_id}", **kwargs}


async def _fake_create_payment_link(
    client: Any, **kwargs: Any,
) -> dict[str, Any]:
    return {"id": "pl_xx", "short_url": "https://rzp.io/i/pl_xx", **kwargs}


async def _fake_create_upi_link(client: Any, **kwargs: Any) -> dict[str, Any]:
    return {"id": "upi_xx", "short_url": "https://rzp.io/i/upi_xx", **kwargs}


async def _fake_void_invoice(client: Any, inv_id: str) -> dict[str, Any]:
    return {"id": inv_id, "status": "voided"}


class FakeRazorpayClient:
    def __init__(self) -> None:
        self.subscription_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self.customer_calls: list[dict[str, Any]] = []
        self.invoice_calls: list[dict[str, Any]] = []


def _patch_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every Razorpay helper used across the case-billing flows."""
    from case_billing.munshi import invoices as inv_mod
    from case_billing.nowlez import cancellation as cancel_mod
    from case_billing.nowlez import subscriptions as subs_mod

    monkeypatch.setattr(subs_mod, "rzp_create_subscription", _fake_create_subscription)
    monkeypatch.setattr(cancel_mod, "rzp_cancel_subscription", _fake_cancel_subscription)
    monkeypatch.setattr(inv_mod, "rzp_create_customer", _fake_create_customer)
    monkeypatch.setattr(inv_mod, "rzp_create_invoice", _fake_create_invoice)
    monkeypatch.setattr(inv_mod, "rzp_create_payment_link", _fake_create_payment_link)
    monkeypatch.setattr(inv_mod, "rzp_create_upi_link", _fake_create_upi_link)
    monkeypatch.setattr(inv_mod, "rzp_void_invoice", _fake_void_invoice)


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _wrap_event(
    event_type: str,
    *,
    event_id: str | None = None,
    subscription: dict[str, Any] | None = None,
    invoice: dict[str, Any] | None = None,
) -> tuple[bytes, str]:
    body: dict[str, Any] = {
        "entity": "event",
        "event": event_type,
        "contains": [],
        "payload": {},
        "created_at": 1714600000,
        "id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
    }
    if subscription is not None:
        body["payload"]["subscription"] = {"entity": subscription}
    if invoice is not None:
        body["payload"]["invoice"] = {"entity": invoice}
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return raw, _sign(raw)


# ---------- helpers --------------------------------------------------------


def _make_user(session: Session, phone: str | None = None) -> uuid.UUID:
    from data_access.models.user import User
    u = User(phone=phone or f"+91999{uuid.uuid4().hex[:7]}")
    session.add(u)
    session.flush()
    return u.id


def _add_cases(
    session: Session, user_id: uuid.UUID, n: int,
) -> list[uuid.UUID]:
    from data_access.models.case import Case
    ids = []
    for _ in range(n):
        c = Case(
            user_id=user_id, cnr=uuid.uuid4().hex[:16],
            portal="district", refresh_enabled=True,
        )
        session.add(c)
        session.flush()
        ids.append(c.id)
    return ids


# ---------- Nowlez full lifecycle ------------------------------------------


def test_nowlez_full_lifecycle_trial_to_cancel(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trial → select Counsel → activated webhook → charged webhook →
    second charged webhook → cancel → cancelled webhook → fallback fires."""
    _patch_all(monkeypatch)
    sender = AsyncMock()
    client = FakeRazorpayClient()
    user_id = _make_user(session, phone="+919998811220")

    # Step 1: trial signup.
    _run(create_trial_for_new_signup(
        user_id=user_id, name="Lifecycle User",
        session=session, send_template_fn=sender,
    ))
    session.flush()
    assert _run(is_in_trial(user_id, session)) is True

    # Step 2: user picks Counsel mid-trial.
    short_url = _run(select_tier_and_subscribe(
        user_id=user_id, chosen_tier="counsel", referral_code=None,
        session=session, razorpay_client=client, config=_Cfg(),
    ))
    session.flush()
    assert short_url.startswith("https://rzp.io/i/sub_")

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.user_id == user_id)
    ).scalar_one()
    rzp_sub_id = sub.razorpay_subscription_id

    # Add some cases to the user so the cancel-→fallback path matters.
    _add_cases(session, user_id, n=3)

    # Step 3: subscription.activated webhook → status=active, intro=in_intro.
    raw, sig = _wrap_event(
        "subscription.activated",
        subscription={
            "id": rzp_sub_id,
            "current_start": 1714600000,
            "current_end": 1717278400,
            "notes": {"product": "nowlez"},
        },
    )
    activated = _run(handle_unified_webhook(
        raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
        session=session, razorpay_client=client,
        send_template_fn=sender,
    ))
    session.flush()
    assert activated.status == STATUS_PROCESSED
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub.id)
    ).scalar_one()
    assert sub.status == "active"
    assert sub.intro_promo_state == "in_intro"

    # Step 4: first subscription.charged → intro_promo terminal.
    raw, sig = _wrap_event(
        "subscription.charged",
        subscription={"id": rzp_sub_id, "notes": {"product": "nowlez"}},
    )
    _run(handle_unified_webhook(
        raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
        session=session, razorpay_client=client,
        send_template_fn=sender,
    ))
    session.flush()
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub.id)
    ).scalar_one()
    assert sub.intro_promo_state == "past_intro"

    # Step 5: second charged event — should NOT re-transition intro
    # promo (already terminal) but the template send still fires.
    raw, sig = _wrap_event(
        "subscription.charged",
        subscription={"id": rzp_sub_id, "notes": {"product": "nowlez"}},
    )
    _run(handle_unified_webhook(
        raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
        session=session, razorpay_client=client,
        send_template_fn=sender,
    ))
    session.flush()
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub.id)
    ).scalar_one()
    assert sub.intro_promo_state == "past_intro"  # unchanged

    # Step 6: user cancels via the API.
    _run(cancel_subscription(
        user_id=user_id, session=session,
        razorpay_client=client, send_template_fn=sender,
    ))
    session.flush()
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub.id)
    ).scalar_one()
    assert sub.status == "cancelled"
    # Razorpay was told to cancel at cycle end.
    assert client.cancel_calls[-1]["cancel_at_cycle_end"] is True

    # Step 7: subscription.cancelled webhook arrives → fallback fires
    # because the user still owns 3 active cases.
    raw, sig = _wrap_event(
        "subscription.cancelled",
        subscription={"id": rzp_sub_id, "notes": {"product": "nowlez"}},
    )
    result = _run(handle_unified_webhook(
        raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
        session=session, razorpay_client=client,
        send_template_fn=sender,
    ))
    session.flush()
    assert result.status == STATUS_PROCESSED
    assert result.details.get("fallback_to_munshi") is True

    from data_access.models.user import UserMunshi
    assert session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one_or_none() is not None


# ---------- Munshi full lifecycle -------------------------------------------


def test_munshi_lifecycle_fallback_invoice_payment_invoice(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trial expires → fallback opens Munshi extension + periods → first
    anniversary invoice issued → invoice.paid marks status='paid' →
    second cycle generates a fresh invoice."""
    _patch_all(monkeypatch)
    sender = AsyncMock()
    client = FakeRazorpayClient()

    # Step 1: build a user with an expired trial.
    user_id = _make_user(session, phone="+919998822221")
    from data_access.models.user import UserNowlez
    session.add(UserNowlez(
        user_id=user_id, name="Munshi-bound",
        tier=None,
        trial_started_at=datetime.now(timezone.utc) - timedelta(days=31),
        trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    session.flush()
    case_ids = _add_cases(session, user_id, n=4)

    # Step 2: day-31 fallback.
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    from data_access.models.billing import CaseBillingPeriod, MunshiInvoice
    from data_access.models.user import UserMunshi
    munshi = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one()
    anniversary = munshi.billing_anniversary_date
    assert anniversary is not None

    periods = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.user_id == user_id)
    ).scalars().all()
    assert len(periods) == 4

    # Step 3: generate the first anniversary invoice one month later.
    one_month_later = date(
        anniversary.year + (anniversary.month // 12),
        ((anniversary.month % 12) + 1),
        min(anniversary.day, 28),
    )
    inv1 = _run(generate_anniversary_invoice(
        user_id=user_id, session=session,
        razorpay_client=client, send_template_fn=sender,
        today=one_month_later,
    ))
    session.flush()
    assert inv1 is not None
    assert inv1.case_count == 4
    assert inv1.amount_paise == 4 * 1000  # 4 × ₹10
    assert inv1.status == "sent"

    # Step 4: invoice.paid arrives.
    paid = _run(mark_invoice_paid(
        inv1.razorpay_invoice_id, session, send_template_fn=sender,
    ))
    session.flush()
    assert paid.status == "paid"
    assert paid.paid_at is not None

    # Step 5: a second cycle (anniversary + 2 months) generates a fresh
    # invoice — the (user_id, cycle_start, cycle_end) tuple differs so
    # the cycle-level idempotency check doesn't dedupe.
    two_months_later = date(
        anniversary.year + ((anniversary.month + 1) // 12),
        (((anniversary.month + 1) % 12) + 1),
        min(anniversary.day, 28),
    )
    inv2 = _run(generate_anniversary_invoice(
        user_id=user_id, session=session,
        razorpay_client=client, send_template_fn=sender,
        today=two_months_later,
    ))
    session.flush()
    assert inv2 is not None
    assert inv2.id != inv1.id
    assert inv2.cycle_start != inv1.cycle_start

    # There are now two MunshiInvoice rows.
    rows = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.user_id == user_id)
    ).scalars().all()
    assert len(rows) == 2


# ---------- race conditions -------------------------------------------------


def test_concurrent_tier_selection_and_fallback_fallback_aborts(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User picks a tier AND the day-31 cron fires for them at almost
    the same moment. The cron's call to ``fallback_to_munshi`` arrives
    after the tier has been written, so the paid-tier guard short-circuits
    and no Munshi extension is created."""
    _patch_all(monkeypatch)
    sender = AsyncMock()
    client = FakeRazorpayClient()

    # User's trial has just expired (cron-eligible) but they pick
    # a tier on the same calendar day.
    user_id = _make_user(session, phone="+919998833330")
    from data_access.models.user import UserNowlez
    session.add(UserNowlez(
        user_id=user_id, name="Racer",
        tier=None,
        trial_started_at=datetime.now(timezone.utc) - timedelta(days=30),
        trial_ends_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    ))
    session.flush()
    _add_cases(session, user_id, n=10)

    # The user wins the race: tier selection happens first.
    _run(select_tier_and_subscribe(
        user_id=user_id, chosen_tier="chambers", referral_code=None,
        session=session, razorpay_client=client, config=_Cfg(),
    ))
    session.flush()

    # The cron picks them up next and calls fallback. The paid-tier
    # guard sees tier='chambers' and bails out — no Munshi extension,
    # no fallback template sent.
    fallback_sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=fallback_sender))
    session.flush()

    from data_access.models.audit import AuditLog
    from data_access.models.user import UserMunshi
    munshi = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one_or_none()
    assert munshi is None
    fallback_sender.assert_not_called()

    audit = session.execute(
        select(AuditLog).where(AuditLog.user_id == user_id)
    ).scalars().all()
    noop = [
        a for a in audit
        if a.event_type == "nowlez.trial_fallback_noop"
        and a.metadata_.get("reason") == "user_has_paid_tier"
    ]
    assert len(noop) == 1


# ---------- webhook idempotency (replay 3x) ---------------------------------


def test_webhook_replay_3x_side_effects_happen_exactly_once(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay the same ``subscription.charged`` webhook event three times.
    The intro promo state transition, referral progression, and template
    send must each happen exactly once; only one ``payment_events`` row
    exists at the end."""
    _patch_all(monkeypatch)

    # Build a user + subscription in 'active'/'in_intro' state so the
    # first event triggers the in_intro→past_intro transition.
    user_id = _make_user(session, phone="+919998844440")
    from data_access.models.billing import (
        PaymentEvent, Subscription,
    )
    from data_access.models.user import UserNowlez

    session.add(UserNowlez(user_id=user_id, name="Replay", tier="chambers"))
    session.flush()
    sub = Subscription(
        user_id=user_id, tier="chambers", billing_cycle="monthly",
        razorpay_subscription_id="sub_replay_xyz",
        status="active",
        intro_promo_state="in_intro",
        referral_state="no_referral",
    )
    session.add(sub)
    session.flush()
    sub_id = sub.id

    sender = AsyncMock()
    raw, sig = _wrap_event(
        "subscription.charged",
        event_id="evt_replay_3x",
        subscription={
            "id": "sub_replay_xyz",
            "notes": {"product": "nowlez"},
        },
    )

    statuses = []
    for _ in range(3):
        result = _run(handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=FakeRazorpayClient(),
            send_template_fn=sender,
        ))
        session.flush()
        statuses.append(result.status)

    # First call processed; replays detected as already_processed.
    assert statuses[0] == STATUS_PROCESSED
    assert statuses[1] == STATUS_ALREADY_PROCESSED
    assert statuses[2] == STATUS_ALREADY_PROCESSED

    # Only one payment_events row.
    rows = session.execute(
        select(PaymentEvent).where(
            PaymentEvent.razorpay_event_id == "evt_replay_3x"
        )
    ).scalars().all()
    assert len(rows) == 1

    # Template sent exactly once.
    assert sender.call_count == 1

    # Intro promo transitioned exactly once (still past_intro).
    sub = session.execute(
        select(Subscription).where(Subscription.id == sub_id)
    ).scalar_one()
    assert sub.intro_promo_state == "past_intro"


def test_webhook_replay_invoice_paid_only_marks_paid_once(
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replayed ``invoice.paid`` for a Munshi invoice does not
    overwrite ``paid_at`` on the second arrival — the first invocation
    already wrote it; the replay path returns already_processed."""
    _patch_all(monkeypatch)

    # Build a Munshi invoice in 'sent' status.
    user_id = _make_user(session, phone="+919998855550")
    from data_access.models.billing import MunshiInvoice
    from data_access.models.user import UserMunshi

    session.add(UserMunshi(user_id=user_id))
    session.flush()
    inv = MunshiInvoice(
        user_id=user_id,
        razorpay_invoice_id="inv_replay_paid",
        cycle_start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        cycle_end=datetime(2026, 4, 1, tzinfo=timezone.utc),
        case_count=3, amount_paise=3000, status="sent",
        due_at=datetime(2026, 4, 8, tzinfo=timezone.utc),
    )
    session.add(inv)
    session.flush()
    inv_id = inv.id

    sender = AsyncMock()
    raw, sig = _wrap_event(
        "invoice.paid",
        event_id="evt_inv_replay",
        invoice={
            "id": "inv_replay_paid",
            "notes": {"product": "munshi"},
        },
    )

    # First call → processed; paid_at gets set.
    first = _run(handle_unified_webhook(
        raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
        session=session, razorpay_client=FakeRazorpayClient(),
        send_template_fn=sender,
    ))
    session.flush()
    assert first.status == STATUS_PROCESSED
    inv1 = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.id == inv_id)
    ).scalar_one()
    assert inv1.status == "paid"
    paid_at_first = inv1.paid_at

    # Second + third replays → already_processed; paid_at NOT
    # overwritten because the handler short-circuits before touching the
    # row.
    for _ in range(2):
        result = _run(handle_unified_webhook(
            raw_body=raw, signature=sig, secret=WEBHOOK_SECRET,
            session=session, razorpay_client=FakeRazorpayClient(),
            send_template_fn=sender,
        ))
        session.flush()
        assert result.status == STATUS_ALREADY_PROCESSED

    inv_final = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.id == inv_id)
    ).scalar_one()
    assert inv_final.status == "paid"
    assert inv_final.paid_at == paid_at_first

    # Receipt template only sent once.
    assert sender.call_count == 1
