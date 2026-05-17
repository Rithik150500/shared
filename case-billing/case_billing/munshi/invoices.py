"""Munshi anniversary invoice generation, payment marking, and void.

This module implements the orchestrator the Munshi anniversary cron
calls once per user-per-cycle. The cron has narrow input — a user_id and
a session — and this module fans out to:

* eligibility check (cross-product, spec Section 2.3)
* anniversary cycle window (Asia/Kolkata day boundaries)
* DISTINCT case count across the cycle
* 200-cap clamp + amount computation (count × ₹10 per case)
* Razorpay customer / invoice / payment-link / UPI-link creation
* ``munshi_invoices`` row insert with the Razorpay IDs persisted
* WhatsApp ``munshi_invoice_payment_v1`` template send

If any step is a no-op (user ineligible, zero cases, or already invoiced
for this cycle) the function returns None and leaves no Razorpay or DB
side-effects behind.

Two more entry points round out the module:

* :func:`mark_invoice_paid` — webhook handler for ``invoice.paid``;
  flips ``status='paid'``, calls :func:`case_billing.munshi.suspension.resume_user`
  if the invoice had been suspended, and sends a receipt template.
* :func:`void_invoice` — administrative path used by sub-project C's
  mid-cycle Nowlez upgrade flow when a Munshi invoice was already
  issued but should be cancelled.

The Razorpay helper functions are imported at module level under
private aliases so unit tests can monkeypatch them without touching the
real HTTP client. The ``send_template_fn`` is passed in by the cron so
the module never has to know about RQ / WhatsApp delivery internals.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from case_billing.errors import InvoiceNotFound
from case_billing.munshi.cycles import compute_cycle_window
from case_billing.munshi.usage import count_billable_cases_in_window
from case_billing.razorpay_client.customers import (
    create_customer as rzp_create_customer,
)
from case_billing.razorpay_client.invoices import (
    create_invoice as rzp_create_invoice,
)
from case_billing.razorpay_client.invoices import (
    void_invoice as rzp_void_invoice,
)
from case_billing.razorpay_client.payment_links import (
    create_payment_link as rzp_create_payment_link,
)
from case_billing.razorpay_client.upi_intent import (
    create_upi_payment_link as rzp_create_upi_link,
)
from case_billing.shared.eligibility import (
    is_user_eligible_for_munshi_billing,
)


# Per-case price in paise (₹10) — duplicated here from BillingConfig to
# keep this module callable without instantiating the config object in
# every cron tick. The BillingConfig default is the source of truth and
# Munshi's deployment script asserts the two match at startup.
MUNSHI_PRICE_PER_CASE_PAISE: int = 1000

# 200-case cap (spec Section 1.2). Same rationale as the price constant.
MUNSHI_CASE_CAP: int = 200

# Default due-by window for postpaid invoices, in days (spec
# ``BillingConfig.invoice_due_days`` default).
MUNSHI_INVOICE_DUE_DAYS: int = 7


class SendTemplateFn(Protocol):
    """Async callable that enqueues a templated WhatsApp message.

    Matches the signature of sub-project B's ``enqueue_send_template``.
    Kept as a Protocol so the cron can pass any compatible wrapper
    (e.g. a stub in tests) without depending on whatsapp-delivery here.
    """

    def __call__(
        self,
        *,
        to: str,
        template: str,
        variables: dict[str, Any],
        brand: str,
    ) -> Awaitable[Any]: ...


async def generate_anniversary_invoice(
    user_id: uuid.UUID,
    session: Session,
    razorpay_client: Any,
    send_template_fn: Callable[..., Awaitable[Any]],
    *,
    today: date | None = None,
) -> Any | None:
    """Generate this cycle's Munshi invoice for ``user_id``.

    Returns the inserted ``MunshiInvoice`` row on success or None when
    the cron should skip this user this cycle (ineligible, no cases, or
    already-billed-this-cycle).

    Args:
        user_id: User to bill.
        session: Open SQLAlchemy session bound to the billing DB.
        razorpay_client: The configured ``RazorpayHTTPClient`` instance;
            passed through to the customer / invoice / payment-link
            wrappers.
        send_template_fn: Async callable used to enqueue the
            ``munshi_invoice_payment_v1`` WhatsApp template; called with
            ``(to=, template=, variables=, brand=)`` keyword args.
        today: Optional override for the IST calendar day used when
            computing the cycle window. The cron passes ``date.today()``
            in production; tests pin a deterministic value.

    No-ops in priority order:

    1. ``is_user_eligible_for_munshi_billing`` returns False → return None.
    2. User has no ``users_munshi.billing_anniversary_date`` set → None.
    3. Zero cases in the cycle window → None (no invoice for "0 × ₹10").
    4. A ``munshi_invoices`` row already exists for this ``(user_id,
       cycle_start, cycle_end)`` → None (idempotent; safe to retry).
    """
    from data_access.models.billing import MunshiInvoice
    from data_access.models.user import User, UserMunshi

    # 1. eligibility (cross-product check).
    if not await is_user_eligible_for_munshi_billing(user_id, session):
        return None

    # 2. anniversary date.
    munshi_row = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one_or_none()
    if munshi_row is None or munshi_row.billing_anniversary_date is None:
        return None

    today_value = today if today is not None else date.today()
    cycle_start, cycle_end = compute_cycle_window(
        anniversary_date=munshi_row.billing_anniversary_date,
        today=today_value,
    )

    # 3. case count.
    case_count = await count_billable_cases_in_window(
        user_id, cycle_start, cycle_end, session,
    )
    if case_count == 0:
        return None

    # 4. cycle-level idempotency. We compare cycle_start/cycle_end
    # values verbatim — the migration UNIQUE constraint enforces it on
    # Postgres but SQLite tests rely on this Python-side check.
    existing = session.execute(
        select(MunshiInvoice)
        .where(MunshiInvoice.user_id == user_id)
        .where(MunshiInvoice.cycle_start == cycle_start)
        .where(MunshiInvoice.cycle_end == cycle_end)
    ).scalar_one_or_none()
    if existing is not None:
        return None

    # 5. clamp at 200 cases for billing math, preserve raw count for audit.
    effective_count = min(case_count, MUNSHI_CASE_CAP)
    amount_paise = effective_count * MUNSHI_PRICE_PER_CASE_PAISE
    amount_rupees = amount_paise // 100

    # Lookup user contact details for Razorpay + WhatsApp.
    user_row = session.execute(
        select(User.phone, User.email).where(User.id == user_id)
    ).first()
    phone = (user_row.phone if user_row else None) or ""
    email = (user_row.email if user_row else None) or ""

    # 6. Razorpay customer (one-shot create — sub-project C will cache
    # the customer_id once it adds the column; for now we create every
    # cycle and let Razorpay dedupe by email/phone).
    customer = await rzp_create_customer(
        razorpay_client,
        name=phone,  # Display name; phone is the only required identifier.
        email=email,
        contact=phone,
        notes={"product": "munshi", "user_id": str(user_id)},
    )
    customer_id = customer["id"]

    due_by = int((datetime.now(timezone.utc) + timedelta(
        days=MUNSHI_INVOICE_DUE_DAYS
    )).timestamp())

    # 7. Razorpay invoice.
    razorpay_invoice = await rzp_create_invoice(
        razorpay_client,
        customer_id=customer_id,
        line_items=[
            {
                "name": f"Munshi case tracking ({effective_count} cases)",
                "amount": amount_paise,
                "currency": "INR",
            }
        ],
        due_by=due_by,
        notes={
            "product": "munshi",
            "user_id": str(user_id),
            "cycle_start": cycle_start.isoformat(),
            "cycle_end": cycle_end.isoformat(),
        },
    )

    # 8. Companion payment links (standard + UPI direct).
    customer_payload = {"name": phone, "contact": phone, "email": email}
    await rzp_create_payment_link(
        razorpay_client,
        amount=amount_paise,
        customer=customer_payload,
        notes={
            "product": "munshi",
            "user_id": str(user_id),
            "razorpay_invoice_id": razorpay_invoice["id"],
        },
    )
    await rzp_create_upi_link(
        razorpay_client,
        amount=amount_paise,
        customer=customer_payload,
        notes={
            "product": "munshi",
            "user_id": str(user_id),
            "razorpay_invoice_id": razorpay_invoice["id"],
        },
    )

    # 9. Persist the local invoice row.
    due_at_dt = datetime.fromtimestamp(due_by, tz=timezone.utc)
    invoice_row = MunshiInvoice(
        user_id=user_id,
        razorpay_invoice_id=razorpay_invoice["id"],
        cycle_start=cycle_start,
        cycle_end=cycle_end,
        case_count=case_count,
        amount_paise=amount_paise,
        status="sent",
        due_at=due_at_dt,
    )
    session.add(invoice_row)
    session.flush()

    # 10. Send the invoice template.
    await send_template_fn(
        to=phone,
        template="munshi_invoice_payment_v1",
        variables={
            "case_count": effective_count,
            "amount_rupees": amount_rupees,
            "due_date": due_at_dt.date().isoformat(),
        },
        brand="munshi",
    )

    return invoice_row


async def mark_invoice_paid(
    razorpay_invoice_id: str,
    session: Session,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> Any:
    """Mark a Munshi invoice paid (webhook handler for ``invoice.paid``).

    Behaviour:

    1. Locate the local invoice row by ``razorpay_invoice_id``; raise
       :class:`case_billing.errors.InvoiceNotFound` if missing (the
       outer webhook router will requeue).
    2. If the row was ``status='suspended'`` (delayed-payment scenario),
       call :func:`case_billing.munshi.suspension.resume_user` to
       reactivate cases.
    3. Update ``status='paid'``, ``paid_at=NOW()``.
    4. Send ``munshi_payment_received_v1`` template.

    Returns the updated MunshiInvoice row.
    """
    from data_access.models.billing import MunshiInvoice
    from data_access.models.user import User

    invoice = session.execute(
        select(MunshiInvoice).where(
            MunshiInvoice.razorpay_invoice_id == razorpay_invoice_id
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise InvoiceNotFound(
            f"No munshi_invoices row for razorpay_invoice_id={razorpay_invoice_id}"
        )

    if invoice.status == "suspended":
        # Lazy import to avoid a circular dependency at module load.
        from case_billing.munshi.suspension import resume_user
        await resume_user(invoice.user_id, session)

    invoice.status = "paid"
    invoice.paid_at = datetime.now(timezone.utc)
    session.flush()

    user_phone = session.execute(
        select(User.phone).where(User.id == invoice.user_id)
    ).scalar_one_or_none()
    if user_phone:
        await send_template_fn(
            to=user_phone,
            template="munshi_payment_received_v1",
            variables={
                "amount_rupees": invoice.amount_paise // 100,
            },
            brand="munshi",
        )

    return invoice


async def void_invoice(
    invoice_id: uuid.UUID,
    session: Session,
    razorpay_client: Any,
    reason: str,
) -> Any:
    """Void a previously-issued Munshi invoice (sub-project C mid-cycle path).

    Used when a Nowlez upgrade lands during the Munshi grace window:
    the un-paid invoice is no longer collectible because the user is
    now on a Nowlez plan.

    Steps:

    1. Lookup invoice; raise :class:`case_billing.errors.InvoiceNotFound`
       if missing or not in a voidable status (``sent`` or ``in_grace``).
    2. Call Razorpay's void endpoint to cancel the invoice upstream.
    3. Set ``status='voided'`` locally.
    4. Audit log ``event_type='munshi.invoice_voided'`` with the reason.
    """
    from data_access.models.audit import AuditLog
    from data_access.models.billing import MunshiInvoice

    invoice = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.id == invoice_id)
    ).scalar_one_or_none()
    if invoice is None:
        raise InvoiceNotFound(f"No munshi_invoices row for id={invoice_id}")
    if invoice.status not in ("sent", "in_grace"):
        raise InvoiceNotFound(
            f"munshi_invoice {invoice_id} not voidable (status={invoice.status!r})"
        )

    if invoice.razorpay_invoice_id:
        await rzp_void_invoice(razorpay_client, invoice.razorpay_invoice_id)

    invoice.status = "voided"
    session.add(
        AuditLog(
            event_type="munshi.invoice_voided",
            user_id=invoice.user_id,
            source="munshi",
            metadata_={"reason": reason, "invoice_id": str(invoice.id)},
        )
    )
    session.flush()
    return invoice
