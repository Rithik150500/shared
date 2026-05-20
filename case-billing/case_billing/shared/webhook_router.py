"""Unified Razorpay webhook router (Task 8.2 / spec Section 2.4).

Razorpay POSTs a single webhook endpoint per merchant; the body's
``event`` field selects which of our internal handlers fires. This
module is the single dispatch point so Munshi and Nowlez share:

* HMAC-SHA256 signature verification (raise :class:`WebhookSignatureInvalid`
  on mismatch — Razorpay treats any non-2xx as "retry me" so we want
  the FastAPI route to surface the 400 explicitly rather than let a
  500 trigger their retry storm).
* Replay idempotency via the ``payment_events`` table (per
  :mod:`case_billing.shared.idempotency`).
* Per-event routing into the Munshi or Nowlez submodules.

The router returns a :class:`WebhookResult` so the calling route can
echo back `{"ok": True, "status": result.status}` and operators can
read the audit trail from `payment_events.payload.__processing_result__`.

Razorpay event types we **handle**:

* ``subscription.activated`` — Nowlez: flip status to ``active``,
  transition intro promo, void any pending Munshi invoices, send
  welcome template.
* ``subscription.charged`` — Nowlez renewal; intro/referral state
  transitions; send renewal template.
* ``subscription.cancelled``, ``subscription.completed`` — Nowlez
  status sync; schedule Munshi fallback if user still owns cases.
* ``subscription.halted``, ``subscription.paused``, ``subscription.resumed``,
  ``subscription.pending`` — Nowlez status sync only.
* ``invoice.paid`` — dispatch by ``notes.product``: Munshi marks
  invoice paid; Nowlez is a no-op (subscription.charged covers it).
* ``invoice.expired`` — Munshi-only: transition to ``in_grace``.

Razorpay event types we **explicitly defer / no-op**:

* ``payment_link.*`` — payment-link state changes are tracked
  indirectly via the invoice they're attached to; we acknowledge the
  event (record_event_processed) so Razorpay stops retrying but take
  no other action. Treated as `status='deferred'` so triage tools
  can surface them.
* ``payment.captured``, ``payment.failed``, ``order.paid`` — covered
  by the invoice/subscription events; we record + ack.
* Anything else — :attr:`WebhookResult.status` becomes
  ``'unhandled_event'``; record + return; no exception.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from case_billing.errors import WebhookSignatureInvalid
from case_billing.razorpay_client.webhooks import (
    parse_webhook_event,
    verify_webhook_signature,
)
from case_billing.shared.idempotency import (
    is_event_already_processed,
    record_event_processed,
)


logger = logging.getLogger(__name__)


# Status codes returned in :class:`WebhookResult.status`. Kept as
# string literals (not an Enum) because callers serialize them
# straight into the FastAPI response body and into JSONB.
STATUS_PROCESSED: str = "processed"
STATUS_ALREADY_PROCESSED: str = "already_processed"
STATUS_UNHANDLED_EVENT: str = "unhandled_event"
STATUS_DEFERRED: str = "deferred"
STATUS_HANDLER_ERROR: str = "handler_error"

# Subscription events that drive Nowlez state. The set is deliberately
# narrow — any unknown subscription.* event falls through to
# ``unhandled_event`` so we get an audit row but no crash.
_SUBSCRIPTION_EVENTS: frozenset[str] = frozenset({
    "subscription.activated",
    "subscription.charged",
    "subscription.cancelled",
    "subscription.completed",
    "subscription.halted",
    "subscription.paused",
    "subscription.resumed",
    "subscription.pending",
    "subscription.updated",
})

# Events we acknowledge (record + return) but take no action on. They
# show up in the audit trail as ``status='deferred'``.
_DEFERRED_EVENTS: frozenset[str] = frozenset({
    "payment_link.paid",
    "payment_link.partially_paid",
    "payment_link.expired",
    "payment_link.cancelled",
    "payment.captured",
    "payment.failed",
    "payment.authorized",
    "order.paid",
})


@dataclass
class WebhookResult:
    """Outcome of a single webhook dispatch.

    Attributes:
        status: One of the ``STATUS_*`` module constants.
        event_id: The Razorpay event id (empty when the body was
            unparseable).
        event_type: The Razorpay ``event`` field.
        product: ``'munshi'`` / ``'nowlez'`` (best-effort inferred from
            event type or payload notes).
        details: Free-form per-handler return data (e.g. invoice id
            that was marked paid).
    """

    status: str
    event_id: str = ""
    event_type: str = ""
    product: str = ""
    details: dict[str, Any] = field(default_factory=dict)


# ---------- public entry point ---------------------------------------------


async def handle_unified_webhook(
    *,
    raw_body: bytes,
    signature: str | None,
    secret: str,
    session: Session,
    razorpay_client: Any,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> WebhookResult:
    """Verify signature, dedupe, dispatch.

    Args:
        raw_body: The exact bytes the FastAPI route received. Must be
            the unparsed body — re-serialized JSON will not verify
            (whitespace/key-order differ).
        signature: Value of the ``X-Razorpay-Signature`` header.
        secret: Webhook secret from the Razorpay dashboard.
        session: Open SQLAlchemy session.
        razorpay_client: Configured ``RazorpayHTTPClient`` — handlers
            that need to make outbound calls (e.g. void) receive this.
        send_template_fn: Sub-project B's ``enqueue_send_template``
            callable; passed through to handlers that send WhatsApp.

    Returns:
        :class:`WebhookResult` describing the dispatch outcome.

    Raises:
        :class:`case_billing.errors.WebhookSignatureInvalid`: when the
            signature does not match. Caller maps this to HTTP 400.
    """
    if not verify_webhook_signature(raw_body, signature, secret):
        # Do NOT record_event_processed: an invalid signature could be
        # an attacker spamming the endpoint; persisting their event ids
        # would let them fill the idempotency table.
        raise WebhookSignatureInvalid(
            "Razorpay webhook signature failed HMAC-SHA256 verification"
        )

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Razorpay webhook body unparseable: %s", exc)
        return WebhookResult(status=STATUS_UNHANDLED_EVENT)

    event = parse_webhook_event(payload)
    product = _infer_product(event.event, event.payload)

    # Idempotency check. If we've seen this event id already, no work
    # to do — return immediately without re-running handlers (the prior
    # run already wrote the audit row).
    if event.id and await is_event_already_processed(event.id, session):
        return WebhookResult(
            status=STATUS_ALREADY_PROCESSED,
            event_id=event.id,
            event_type=event.event,
            product=product,
        )

    # Dispatch.
    result = await _dispatch(
        event_type=event.event,
        payload=event.payload,
        product=product,
        session=session,
        razorpay_client=razorpay_client,
        send_template_fn=send_template_fn,
    )
    result.event_id = event.id
    result.event_type = event.event
    result.product = product

    # Persist the idempotency + audit row. If the row already exists
    # (concurrent worker raced us) record_event_processed returns
    # False and we mark the result as already_processed so the audit
    # trail is consistent. The handler side-effects are still applied —
    # they had to be idempotent themselves (per spec).
    inserted = await record_event_processed(
        razorpay_event_id=event.id,
        event_type=event.event,
        product=product,
        payload=payload,
        session=session,
        processing_result={
            "status": result.status,
            "details": result.details,
        },
    )
    if not inserted and result.status == STATUS_PROCESSED:
        # Race: another worker recorded the event first. Downgrade
        # our reported status so the caller knows the audit row may
        # already reflect the prior run.
        result.status = STATUS_ALREADY_PROCESSED

    return result


# ---------- dispatcher ------------------------------------------------------


async def _dispatch(
    *,
    event_type: str,
    payload: dict[str, Any],
    product: str,
    session: Session,
    razorpay_client: Any,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> WebhookResult:
    """Route by event type. Catches handler errors so the outer caller
    still records an audit row (otherwise Razorpay would retry forever).
    """
    try:
        if event_type == "subscription.activated":
            details = await _handle_subscription_activated(
                payload, session, razorpay_client, send_template_fn,
            )
            return WebhookResult(status=STATUS_PROCESSED, details=details)

        if event_type == "subscription.charged":
            details = await _handle_subscription_charged(
                payload, session, send_template_fn,
            )
            return WebhookResult(status=STATUS_PROCESSED, details=details)

        if event_type in ("subscription.cancelled", "subscription.completed"):
            details = await _handle_subscription_terminated(
                payload, session, send_template_fn,
            )
            return WebhookResult(status=STATUS_PROCESSED, details=details)

        if event_type in (
            "subscription.halted",
            "subscription.paused",
            "subscription.resumed",
            "subscription.pending",
            "subscription.updated",
        ):
            details = await _handle_subscription_status_sync(
                event_type, payload, session,
            )
            return WebhookResult(status=STATUS_PROCESSED, details=details)

        if event_type == "invoice.paid":
            details = await _handle_invoice_paid(
                payload, session, send_template_fn,
            )
            return WebhookResult(status=STATUS_PROCESSED, details=details)

        if event_type == "invoice.expired":
            details = await _handle_invoice_expired(payload, session)
            return WebhookResult(status=STATUS_PROCESSED, details=details)

        if event_type in _DEFERRED_EVENTS:
            logger.info(
                "Razorpay event %s deferred — recorded but no handler",
                event_type,
            )
            return WebhookResult(status=STATUS_DEFERRED)

        logger.info(
            "Razorpay event %s unhandled — recorded for audit only",
            event_type,
        )
        return WebhookResult(status=STATUS_UNHANDLED_EVENT)
    except Exception as exc:  # pragma: no cover - defensive only
        # We catch broadly so the audit row still gets written. The
        # exception message lands in the JSONB so triage can find it.
        logger.exception(
            "Razorpay webhook handler for %s raised: %s", event_type, exc,
        )
        return WebhookResult(
            status=STATUS_HANDLER_ERROR,
            details={"error": str(exc), "exc_type": exc.__class__.__name__},
        )


# ---------- per-event handlers ---------------------------------------------


async def _handle_subscription_activated(
    payload: dict[str, Any],
    session: Session,
    razorpay_client: Any,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> dict[str, Any]:
    """Handle ``subscription.activated``.

    Side-effects (spec Section 2.4):
    1. Flip ``subscriptions.status='active'`` + ``intro_promo_state='in_intro'``.
    2. Stamp ``period_start`` / ``period_end`` from Razorpay's
       ``current_start`` / ``current_end`` (Unix seconds → UTC).
    3. If subscription has a linked referral, ``apply_referral_at_first_payment``.
    4. **Mid-cycle Munshi voiding**: void any pending Munshi invoices
       for this user (status sent/in_grace) — they're now on Nowlez and
       won't be billed Munshi-style. Close active ``case_billing_periods``.
    5. Send ``nowlez_subscription_started_v1`` template.
    """
    from datetime import datetime, timezone

    from data_access.models.billing import (
        CaseBillingPeriod,
        MunshiInvoice,
        Subscription,
    )
    from data_access.models.user import User

    from case_billing.munshi.invoices import void_invoice
    from case_billing.nowlez.promos import transition_intro_promo_state
    from case_billing.nowlez.referrals import (
        apply_referral_at_first_payment,
    )
    from case_billing.nowlez.upsell import record_upgrade_conversion

    sub_payload = _extract_subscription_payload(payload)
    rzp_sub_id = sub_payload.get("id")
    if not rzp_sub_id:
        return {"reason": "missing_razorpay_subscription_id"}

    sub = session.execute(
        select(Subscription).where(
            Subscription.razorpay_subscription_id == rzp_sub_id
        )
    ).scalar_one_or_none()
    if sub is None:
        return {"reason": "subscription_not_found", "rzp_sub_id": rzp_sub_id}

    user_id = sub.user_id
    details: dict[str, Any] = {
        "subscription_id": str(sub.id),
        "user_id": str(user_id),
    }

    # 1+2. Status + period stamps.
    sub.status = "active"
    current_start = sub_payload.get("current_start")
    current_end = sub_payload.get("current_end")
    if current_start is not None:
        sub.period_start = datetime.fromtimestamp(
            int(current_start), tz=timezone.utc,
        )
    if current_end is not None:
        sub.period_end = datetime.fromtimestamp(
            int(current_end), tz=timezone.utc,
        )
    session.flush()

    # Intro promo transition. The state-machine helper is the canonical
    # path; it raises BillingError on illegal transitions, but our
    # outer try/except surfaces those into the audit row.
    if sub.intro_promo_state == "pre_first_payment":
        await transition_intro_promo_state(
            sub.id, "subscription.activated", session,
        )
        details["intro_promo_state"] = sub.intro_promo_state

    # 3. Referral wiring (if subscription was created with one).
    referral_id_str = (sub_payload.get("notes") or {}).get("referral_id")
    if referral_id_str:
        try:
            referral_id = uuid.UUID(referral_id_str)
        except (ValueError, TypeError):
            referral_id = None
        if referral_id is not None:
            await apply_referral_at_first_payment(
                referral_id=referral_id,
                referred_subscription_id=sub.id,
                session=session,
            )
            details["referral_linked"] = True

    # 4. Void any pending Munshi invoices (mid-cycle upgrade path).
    voidable = session.execute(
        select(MunshiInvoice)
        .where(MunshiInvoice.user_id == user_id)
        .where(MunshiInvoice.status.in_(("sent", "in_grace")))
    ).scalars().all()
    voided_ids: list[str] = []
    for inv in voidable:
        await void_invoice(
            invoice_id=inv.id,
            session=session,
            razorpay_client=razorpay_client,
            reason="nowlez_upgrade",
        )
        voided_ids.append(str(inv.id))
    details["munshi_invoices_voided"] = voided_ids

    # Close any open billing periods so the user isn't double-billed.
    now = datetime.now(timezone.utc)
    open_periods = session.execute(
        select(CaseBillingPeriod)
        .where(CaseBillingPeriod.user_id == user_id)
        .where(CaseBillingPeriod.period_end.is_(None))
    ).scalars().all()
    for period in open_periods:
        period.period_end = now
    session.flush()
    details["billing_periods_closed"] = len(open_periods)

    # Sub-project C: stamp the most-recent unconverted upsell event so
    # the analytics layer can attribute this conversion to its trigger.
    # No-op when the user never received an upsell (direct signup path).
    record_upgrade_conversion(session, user_id=user_id, tier=sub.tier)
    session.flush()

    # 5. Send welcome template.
    phone = session.execute(
        select(User.phone).where(User.id == user_id)
    ).scalar_one_or_none()
    if phone:
        await send_template_fn(
            to=phone,
            template="nowlez_subscription_started_v1",
            variables={"tier": sub.tier},
            brand="nowlez",
        )

    return details


async def _handle_subscription_charged(
    payload: dict[str, Any],
    session: Session,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> dict[str, Any]:
    """Handle ``subscription.charged`` (renewal billed successfully).

    Side-effects:
    1. Transition intro_promo_state ``in_intro → past_intro`` (terminal).
    2. Referral cycle progression:
       - ``pending_mutual`` → run :func:`apply_referree_2nd_month_discount`
         and :func:`schedule_referrer_mutual_benefit`; this leaves the
         state in ``mutual_applied``.
       - ``mutual_applied`` → call :func:`mark_referrer_mutual_applied`
         on the referrer side.
    3. Send ``nowlez_renewal_success_v1``.
    """
    from data_access.models.billing import Referral, Subscription
    from data_access.models.user import User

    from case_billing.nowlez.referrals import (
        apply_referree_2nd_month_discount,
        mark_referrer_mutual_applied,
        schedule_referrer_mutual_benefit,
    )

    sub_payload = _extract_subscription_payload(payload)
    rzp_sub_id = sub_payload.get("id")
    if not rzp_sub_id:
        return {"reason": "missing_razorpay_subscription_id"}

    sub = session.execute(
        select(Subscription).where(
            Subscription.razorpay_subscription_id == rzp_sub_id
        )
    ).scalar_one_or_none()
    if sub is None:
        return {"reason": "subscription_not_found", "rzp_sub_id": rzp_sub_id}

    details: dict[str, Any] = {
        "subscription_id": str(sub.id),
        "user_id": str(sub.user_id),
    }

    # 1. Intro promo terminal transition.
    if sub.intro_promo_state == "in_intro":
        # Use the direct setter rather than transition_intro_promo_state
        # because the state machine only fires on the first
        # subscription.charged; subsequent charges are inert and we
        # don't want them to raise.
        from case_billing.nowlez.promos import record_intro_promo_consumed
        await record_intro_promo_consumed(sub.id, session)
        details["intro_promo_state"] = sub.intro_promo_state

    # 2. Referral progression.
    if sub.referral_state == "pending_mutual":
        applied = await apply_referree_2nd_month_discount(sub.id, session)
        if applied:
            # Find the referral row to schedule the referrer's benefit.
            referral = session.execute(
                select(Referral).where(Referral.referred_subscription_id == sub.id)
            ).scalar_one_or_none()
            if referral is not None:
                await schedule_referrer_mutual_benefit(referral.id, session)
                details["referrer_benefit_scheduled"] = True
    elif sub.referral_state == "mutual_applied":
        referral = session.execute(
            select(Referral).where(Referral.referred_subscription_id == sub.id)
        ).scalar_one_or_none()
        if referral is not None:
            await mark_referrer_mutual_applied(referral.id, session)
            details["referrer_mutual_marked"] = True

    # 3. Send renewal template.
    phone = session.execute(
        select(User.phone).where(User.id == sub.user_id)
    ).scalar_one_or_none()
    if phone:
        await send_template_fn(
            to=phone,
            template="nowlez_renewal_success_v1",
            variables={"tier": sub.tier},
            brand="nowlez",
        )

    return details


async def _handle_subscription_terminated(
    payload: dict[str, Any],
    session: Session,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> dict[str, Any]:
    """Handle ``subscription.cancelled`` / ``subscription.completed``.

    Flip status, and if the user still owns cases, schedule Munshi
    fallback so they can keep being served.
    """
    from data_access.models.billing import Subscription
    from data_access.models.case import Case

    from case_billing.nowlez.fallback import fallback_to_munshi

    sub_payload = _extract_subscription_payload(payload)
    rzp_sub_id = sub_payload.get("id")
    if not rzp_sub_id:
        return {"reason": "missing_razorpay_subscription_id"}

    sub = session.execute(
        select(Subscription).where(
            Subscription.razorpay_subscription_id == rzp_sub_id
        )
    ).scalar_one_or_none()
    if sub is None:
        return {"reason": "subscription_not_found", "rzp_sub_id": rzp_sub_id}

    details: dict[str, Any] = {
        "subscription_id": str(sub.id),
        "user_id": str(sub.user_id),
    }

    if sub.status not in ("cancelled", "expired"):
        sub.status = "cancelled"
        session.flush()

    # If the user still has refresh-enabled cases, hand them off to
    # Munshi so service continues. fallback_to_munshi is idempotent.
    has_cases = session.execute(
        select(Case.id)
        .where(Case.user_id == sub.user_id)
        .where(Case.refresh_enabled.is_(True))
        .limit(1)
    ).first()
    if has_cases is not None:
        await fallback_to_munshi(sub.user_id, session, send_template_fn)
        details["fallback_to_munshi"] = True

    return details


async def _handle_subscription_status_sync(
    event_type: str,
    payload: dict[str, Any],
    session: Session,
) -> dict[str, Any]:
    """Handle status-only subscription events (``halted`` / ``paused`` / etc.).

    These don't change billing semantics — they just mirror Razorpay's
    view of the subscription. We update local ``status`` so admin
    tooling can see it without polling Razorpay.
    """
    from data_access.models.billing import Subscription

    sub_payload = _extract_subscription_payload(payload)
    rzp_sub_id = sub_payload.get("id")
    if not rzp_sub_id:
        return {"reason": "missing_razorpay_subscription_id"}

    sub = session.execute(
        select(Subscription).where(
            Subscription.razorpay_subscription_id == rzp_sub_id
        )
    ).scalar_one_or_none()
    if sub is None:
        return {"reason": "subscription_not_found", "rzp_sub_id": rzp_sub_id}

    details: dict[str, Any] = {
        "subscription_id": str(sub.id),
        "event": event_type,
    }

    if event_type == "subscription.halted":
        sub.status = "past_due"
    elif event_type == "subscription.paused":
        # No 'paused' in our status CHECK; map to past_due for visibility.
        sub.status = "past_due"
    elif event_type == "subscription.resumed":
        sub.status = "active"
    # subscription.pending / subscription.updated have no status change.
    session.flush()
    details["status"] = sub.status
    return details


async def _handle_invoice_paid(
    payload: dict[str, Any],
    session: Session,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> dict[str, Any]:
    """Handle ``invoice.paid``. Dispatch by ``notes.product``.

    * ``'munshi'`` → :func:`case_billing.munshi.invoices.mark_invoice_paid`.
    * ``'nowlez'`` → no-op (the Subscriptions API already triggered
      ``subscription.charged`` which is the canonical Nowlez signal).
    * Anything else → no-op + log.
    """
    from case_billing.munshi.invoices import mark_invoice_paid

    invoice_payload = _extract_invoice_payload(payload)
    rzp_invoice_id = invoice_payload.get("id")
    notes = invoice_payload.get("notes") or {}
    product = notes.get("product", "")

    if not rzp_invoice_id:
        return {"reason": "missing_razorpay_invoice_id"}

    if product == "munshi":
        invoice = await mark_invoice_paid(
            rzp_invoice_id, session, send_template_fn,
        )
        return {
            "product": "munshi",
            "razorpay_invoice_id": rzp_invoice_id,
            "munshi_invoice_id": str(invoice.id),
        }

    if product == "nowlez":
        return {
            "product": "nowlez",
            "noop_reason": "covered_by_subscription_charged",
        }

    return {
        "razorpay_invoice_id": rzp_invoice_id,
        "noop_reason": "unknown_product",
        "product": product,
    }


async def _handle_invoice_expired(
    payload: dict[str, Any],
    session: Session,
) -> dict[str, Any]:
    """Handle ``invoice.expired``. Munshi: transition to ``in_grace``.

    Nowlez doesn't generate ``invoice.expired`` (subscriptions handle
    failure via subscription.halted) so this is Munshi-only in practice.
    """
    from data_access.models.billing import MunshiInvoice

    invoice_payload = _extract_invoice_payload(payload)
    rzp_invoice_id = invoice_payload.get("id")
    notes = invoice_payload.get("notes") or {}
    product = notes.get("product", "")

    if product != "munshi" or not rzp_invoice_id:
        return {
            "noop_reason": "not_munshi_invoice",
            "product": product,
            "razorpay_invoice_id": rzp_invoice_id or "",
        }

    inv = session.execute(
        select(MunshiInvoice).where(
            MunshiInvoice.razorpay_invoice_id == rzp_invoice_id
        )
    ).scalar_one_or_none()
    if inv is None:
        return {
            "noop_reason": "invoice_not_found",
            "razorpay_invoice_id": rzp_invoice_id,
        }

    if inv.status == "sent":
        inv.status = "in_grace"
        session.flush()
        return {
            "munshi_invoice_id": str(inv.id),
            "new_status": "in_grace",
        }
    return {
        "munshi_invoice_id": str(inv.id),
        "noop_reason": "status_not_sent",
        "current_status": inv.status,
    }


# ---------- payload helpers -------------------------------------------------


def _extract_subscription_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the subscription entity out of a Razorpay subscription event.

    Razorpay nests entities at ``payload.subscription.entity`` (and
    sometimes ``payload.payment.entity`` alongside on charged events).
    """
    sub_wrap = (payload.get("subscription") or {})
    return dict(sub_wrap.get("entity") or {})


def _extract_invoice_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the invoice entity out of a Razorpay invoice event."""
    inv_wrap = (payload.get("invoice") or {})
    return dict(inv_wrap.get("entity") or {})


def _infer_product(event_type: str, payload: dict[str, Any]) -> str:
    """Best-effort guess at the product an event belongs to.

    Used purely for triage (``payment_events.product`` CHECK constraint
    accepts ``'munshi'`` and ``'nowlez'``). Subscription events are
    always Nowlez; invoice events look at the notes; payment_link
    events default to munshi (Nowlez doesn't use payment links).
    """
    if event_type.startswith("subscription."):
        return "nowlez"
    if event_type.startswith("invoice."):
        notes = (_extract_invoice_payload(payload) or {}).get("notes") or {}
        product = notes.get("product")
        if product in ("munshi", "nowlez"):
            return product
        return "munshi"  # default for invoices (Munshi's the heavier user)
    if event_type.startswith("payment_link."):
        return "munshi"
    if event_type.startswith("payment.") or event_type.startswith("order."):
        # These can be either; prefer munshi because Nowlez's payment
        # events get classified via subscription.charged.
        return "munshi"
    # Fall back to munshi so the CHECK constraint passes.
    return "munshi"


# Public alias: spec Section 2.4 refers to the entry point as
# ``route_event`` in the high-level architecture diagram; the
# implementation uses ``handle_unified_webhook`` because the full
# function does signature verification + idempotency + dispatch, but
# external callers can import either name.
route_event = handle_unified_webhook


__all__ = [
    "WebhookResult",
    "handle_unified_webhook",
    "route_event",
    "STATUS_PROCESSED",
    "STATUS_ALREADY_PROCESSED",
    "STATUS_UNHANDLED_EVENT",
    "STATUS_DEFERRED",
    "STATUS_HANDLER_ERROR",
]
