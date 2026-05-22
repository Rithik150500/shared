"""End-to-end Razorpay sandbox test (Task 2.1, sub-project E).

Exercises the full Munshi billing happy path against the real Razorpay
sandbox: ``generate_anniversary_invoice`` → hand-crafted ``invoice.paid``
webhook → ``handle_unified_webhook`` → DB state flip → WhatsApp template
enqueue → idempotency replay.

Why "hand-crafted" webhook? Razorpay's sandbox does not retroactively
emit ``invoice.paid`` webhooks for arbitrary invoices we mark paid via
the dashboard, and we cannot bind a real public webhook URL from CI.
The hand-crafted payload mirrors Razorpay's documented event shape and
is HMAC-signed with the sandbox webhook secret, so the verification +
dispatch path is exercised exactly as production sees it. The bullet
the hand-crafted bypass leaves open ("does Razorpay's *actual* webhook
delivery look like what we expect?") is covered by the one-shot manual
smoke listed in ``tests/live/README.md`` — pre-launch checklist.

Skipped by default unless ``RAZORPAY_SANDBOX_*`` env vars are set; see
:fixture:`sandbox_creds` in ``conftest.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from case_billing.munshi.api import generate_anniversary_invoice
from case_billing.razorpay_client.client import RazorpayHTTPClient
from case_billing.razorpay_client.invoices import (
    fetch_invoice as rzp_fetch_invoice,
    void_invoice as rzp_void_invoice,
)
from case_billing.shared.webhook_router import (
    STATUS_ALREADY_PROCESSED,
    STATUS_PROCESSED,
    handle_unified_webhook,
)


pytestmark = pytest.mark.live


# ---------- session + helpers ----------------------------------------------


@pytest.fixture()
def session() -> Session:
    """In-memory SQLite session that mirrors the integration-test pattern.

    Lives at fixture (not module) scope so each test gets a clean slate
    even though the live suite only has one test today — keeps room for
    follow-on e2e cases without cross-talk.
    """
    from data_access.base import Base

    import data_access.models.auth  # noqa: F401
    import data_access.models.billing  # noqa: F401
    import data_access.models.case  # noqa: F401
    import data_access.models.user  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _run(coro):
    """Drive ``coro`` to completion on a fresh event loop.

    Mirrors the helper used by the integration suite so this file's
    test body stays synchronous (and any test failure points straight
    at the relevant ``await`` line rather than at the loop runner).
    """
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _sign(body: bytes, secret: str) -> str:
    """Compute Razorpay's HMAC-SHA256 hex digest for ``body``.

    Matches :func:`case_billing.razorpay_client.webhooks.verify_webhook_signature`
    byte-for-byte — same algorithm, same hex-digest format, same input
    bytes (the un-reserialized JSON body).
    """
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _make_munshi_user(
    session: Session,
    *,
    anniversary: date,
    phone: str | None = None,
) -> uuid.UUID:
    """Create User + UserMunshi.

    Copied verbatim from ``tests/integration/test_munshi_invoice_cycle.py``
    rather than imported to avoid the test-as-module gymnastics; the
    helper is six lines and lifting it keeps this file self-contained
    if/when the integration helpers move.
    """
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
    """Insert ``n`` cases each with an active billing period since ``period_start``."""
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


def _build_invoice_paid_payload(
    *,
    razorpay_invoice_id: str,
    user_id: uuid.UUID,
    amount_paise: int,
    event_id: str,
) -> dict[str, Any]:
    """Hand-craft a minimal ``invoice.paid`` webhook body.

    Only the fields the router actually inspects are populated:

    * top-level ``id`` — used for idempotency lookup against
      ``payment_events.razorpay_event_id``.
    * top-level ``event`` — dispatcher key.
    * ``payload.invoice.entity.id`` — passed to ``mark_invoice_paid``.
    * ``payload.invoice.entity.notes.product`` — dispatcher fans out by
      this; must be ``"munshi"`` for the Munshi branch.

    The other fields (``status``, ``amount_paid``, ``account_id``) are
    set to plausible values so debugging tools that dump the payload
    don't show a stub-looking document.
    """
    return {
        "entity": "event",
        "account_id": "acc_sandbox_test",
        "event": "invoice.paid",
        "contains": ["invoice", "payment"],
        "payload": {
            "invoice": {
                "entity": {
                    "id": razorpay_invoice_id,
                    "entity": "invoice",
                    "status": "paid",
                    "amount": amount_paise,
                    "amount_paid": amount_paise,
                    "currency": "INR",
                    "notes": {
                        "product": "munshi",
                        "user_id": str(user_id),
                    },
                }
            },
        },
        "created_at": int(datetime.now(timezone.utc).timestamp()),
        "id": event_id,
    }


# ---------- the e2e test ----------------------------------------------------


def test_munshi_invoice_paid_e2e_against_sandbox(
    session: Session,
    sandbox_creds: dict[str, str],
) -> None:
    """Invoice generation → webhook → paid → enqueue → idempotency.

    Steps (mirrors task 2.1 spec):

    1. Set up a fresh Munshi user with three active case billing
       periods. Three cases × ₹10 = ₹30 = 3000 paise.
    2. Call ``generate_anniversary_invoice`` against the *real* sandbox
       Razorpay HTTP client. This issues a customer, an invoice, a
       payment link, and a UPI intent link against the sandbox.
    3. Round-trip via ``fetch_invoice`` to confirm Razorpay actually
       persisted the row (catches any auth or sandbox-region misroutes).
    4. Hand-craft an ``invoice.paid`` body referencing the
       ``razorpay_invoice_id`` and HMAC-sign with the sandbox webhook
       secret.
    5. Call ``handle_unified_webhook`` directly (the FastAPI route lives
       in Nowlez; case-billing exposes the pure dispatch function).
    6. Assert ``MunshiInvoice.status == 'paid'``, ``paid_at`` set, and
       a ``PaymentEvent`` audit row exists.
    7. Assert ``send_template_fn`` was called once with the
       ``munshi_payment_received_v1`` template (the receipt) — the
       ``munshi_invoice_payment_v1`` template was the initial bill,
       sent during step 2.
    8. Replay the webhook. Assert no new PaymentEvent row, no new
       template send (idempotency via unique constraint on
       ``razorpay_event_id``).
    9. Tear down the sandbox invoice via ``void_invoice`` so the next
       test run is not polluted, and delete the local DB rows.
    """
    from data_access.models.billing import (
        CaseBillingPeriod,
        MunshiInvoice,
        PaymentEvent,
    )
    from data_access.models.case import Case
    from data_access.models.user import User, UserMunshi

    # 1. User + cases. Use a today/anniversary pair that puts every case
    # safely inside the cycle window with no DST surprises (Asia/Kolkata
    # has no DST anyway). today=15th, anniversary=15th of prior month so
    # cycle is the full prior month — cases opened on the 1st of that
    # month are billable across the whole window.
    today = date(2026, 4, 15)
    anniversary = date(2026, 3, 15)
    period_start = datetime(2026, 3, 1, tzinfo=timezone.utc)

    user_id = _make_munshi_user(session, anniversary=anniversary)
    _open_case_periods(session, user_id, n=3, period_start=period_start)

    # 2. Real sandbox client. Constructor takes (key_id, key_secret) —
    # see RazorpayHTTPClient.__init__. The 30s timeout is the
    # package-wide default; sandbox calls are typically sub-second.
    rzp_client = RazorpayHTTPClient(
        key_id=sandbox_creds["api_key"],
        key_secret=sandbox_creds["api_secret"],
    )

    send_template_fn = AsyncMock(return_value="wamid_sandbox_test")

    razorpay_invoice_id: str | None = None
    try:
        invoice_row = _run(
            generate_anniversary_invoice(
                user_id=user_id,
                session=session,
                razorpay_client=rzp_client,
                send_template_fn=send_template_fn,
                today=today,
            )
        )
        session.flush()

        # Local row exists with the Razorpay id stamped.
        assert invoice_row is not None, (
            "generate_anniversary_invoice returned None; check sandbox "
            "credentials and that the cross-product eligibility gate passes "
            "(the user is fresh so it should)."
        )
        assert invoice_row.status == "sent"
        assert invoice_row.case_count == 3
        assert invoice_row.amount_paise == 3 * 1000  # 3 cases × ₹10
        assert invoice_row.razorpay_invoice_id is not None
        razorpay_invoice_id = invoice_row.razorpay_invoice_id

        # 3. Sandbox-side round-trip. fetch_invoice raises RazorpayApiError
        # on 4xx/5xx, so a returned dict alone is a strong "row exists"
        # signal. We also check the id matches what we just got back.
        sandbox_invoice = _run(
            rzp_fetch_invoice(rzp_client, razorpay_invoice_id)
        )
        assert sandbox_invoice["id"] == razorpay_invoice_id
        # Sandbox issues invoices in "issued" status until manually paid;
        # we don't pay through Razorpay's UI in this test so don't assert
        # on the sandbox-side status here — the *local* status flip is
        # what mark_invoice_paid drives, and the sandbox row's status is
        # tangential to our DB transition assertions below.

        # The initial bill template was sent during invoice generation.
        # Record its call count so the post-webhook assertion can
        # distinguish the receipt template from the bill template.
        assert send_template_fn.await_count == 1
        bill_call = send_template_fn.await_args_list[0]
        assert bill_call.kwargs["template"] == "munshi_invoice_payment_v1"

        # 4. Hand-craft the invoice.paid event and HMAC-sign it.
        event_id = f"evt_e2e_{uuid.uuid4().hex[:16]}"
        body_dict = _build_invoice_paid_payload(
            razorpay_invoice_id=razorpay_invoice_id,
            user_id=user_id,
            amount_paise=invoice_row.amount_paise,
            event_id=event_id,
        )
        # separators=(",", ":") gives a deterministic body byte-string so
        # the HMAC over body and the body bytes the router parses are
        # the same bytes — see webhook_router.verify_webhook_signature
        # docstring re: whitespace sensitivity.
        raw_body = json.dumps(body_dict, separators=(",", ":")).encode("utf-8")
        signature = _sign(raw_body, sandbox_creds["webhook_secret"])

        # 5. Dispatch through the unified router.
        result = _run(
            handle_unified_webhook(
                raw_body=raw_body,
                signature=signature,
                secret=sandbox_creds["webhook_secret"],
                session=session,
                razorpay_client=rzp_client,
                send_template_fn=send_template_fn,
            )
        )
        session.flush()

        # 6. DB transitions: local invoice → paid; PaymentEvent row exists.
        assert result.status == STATUS_PROCESSED
        assert result.event_id == event_id
        assert result.event_type == "invoice.paid"
        assert result.product == "munshi"

        session.refresh(invoice_row)
        assert invoice_row.status == "paid"
        assert invoice_row.paid_at is not None

        payment_event_rows = session.execute(
            select(PaymentEvent).where(
                PaymentEvent.razorpay_event_id == event_id
            )
        ).scalars().all()
        assert len(payment_event_rows) == 1
        pe = payment_event_rows[0]
        assert pe.event_type == "invoice.paid"
        assert pe.product == "munshi"

        # 7. Receipt template enqueued: bill (count=1) + receipt (count=2).
        assert send_template_fn.await_count == 2
        receipt_call = send_template_fn.await_args_list[1]
        assert receipt_call.kwargs["template"] == "munshi_payment_received_v1"
        assert receipt_call.kwargs["brand"] == "munshi"
        assert receipt_call.kwargs["variables"]["amount_rupees"] == 30

        # 8. Idempotency replay — same body, same signature, same event_id.
        # The unique constraint on payment_events.razorpay_event_id is the
        # safety net; the router short-circuits via is_event_already_processed
        # before re-running the handler.
        replay_result = _run(
            handle_unified_webhook(
                raw_body=raw_body,
                signature=signature,
                secret=sandbox_creds["webhook_secret"],
                session=session,
                razorpay_client=rzp_client,
                send_template_fn=send_template_fn,
            )
        )
        session.flush()

        assert replay_result.status == STATUS_ALREADY_PROCESSED
        assert replay_result.event_id == event_id

        # Still exactly one PaymentEvent row.
        payment_event_rows_after = session.execute(
            select(PaymentEvent).where(
                PaymentEvent.razorpay_event_id == event_id
            )
        ).scalars().all()
        assert len(payment_event_rows_after) == 1

        # Still exactly two template sends (replay did NOT enqueue another).
        assert send_template_fn.await_count == 2

    finally:
        # 9. Cleanup.
        # 9a. Best-effort sandbox void so subsequent runs start clean.
        # The sandbox happily voids "issued" invoices; we ignore errors
        # (e.g. invoice was already paid via dashboard) to ensure the
        # local cleanup below still runs.
        if razorpay_invoice_id is not None:
            try:
                _run(rzp_void_invoice(rzp_client, razorpay_invoice_id))
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask test errors
                print(f"warning: void_invoice failed during cleanup: {exc!r}")

        # 9b. Local DB cleanup. Order matters: child rows before parents.
        session.execute(
            delete(MunshiInvoice).where(MunshiInvoice.user_id == user_id)
        )
        session.execute(
            delete(CaseBillingPeriod).where(
                CaseBillingPeriod.user_id == user_id
            )
        )
        session.execute(delete(Case).where(Case.user_id == user_id))
        session.execute(delete(UserMunshi).where(UserMunshi.user_id == user_id))
        # PaymentEvent isn't user-scoped (no user_id column), so target by
        # the event ids we just minted. There's only one event per run.
        if "event_id" in locals():
            session.execute(
                delete(PaymentEvent).where(
                    PaymentEvent.razorpay_event_id == event_id
                )
            )
        session.execute(delete(User).where(User.id == user_id))
        session.flush()
