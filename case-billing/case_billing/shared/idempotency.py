"""``payment_events`` idempotency log (Task 8.1).

Razorpay retries webhooks aggressively (every minute for ~24h) until it
sees a 2xx, so every handler must be safe against duplicate delivery.
The strategy:

1. Compute a stable id for the event — Razorpay assigns one as the
   top-level ``id`` field on every webhook body (string like
   ``"evt_xxx"``). That id is what we persist.
2. Before doing any work, check :func:`is_event_already_processed`
   against ``payment_events.razorpay_event_id``.
3. After the handler succeeds, call :func:`record_event_processed` to
   INSERT the row. The UNIQUE index on ``razorpay_event_id`` is the
   safety net for the race where two workers pick up the same event
   between the SELECT and the INSERT — the loser raises ``IntegrityError``
   and the caller treats it the same as "already seen".

These two functions are deliberately the *only* surface; the webhook
router decides when to call them. We use the sync SQLAlchemy ``Session``
because the rest of the package does — no event-loop hops here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


async def is_event_already_processed(
    razorpay_event_id: str,
    session: Session,
) -> bool:
    """Return True iff ``payment_events`` already has a row for this event id.

    Args:
        razorpay_event_id: The top-level ``id`` field from the Razorpay
            webhook payload.
        session: Open SQLAlchemy session bound to the billing DB.

    Returns:
        True if a row exists; False otherwise (or when ``razorpay_event_id``
        is empty — the caller logged an error already so we don't
        double-log here).
    """
    from data_access.models.billing import PaymentEvent

    if not razorpay_event_id:
        return False

    row = session.execute(
        select(PaymentEvent.id)
        .where(PaymentEvent.razorpay_event_id == razorpay_event_id)
        .limit(1)
    ).first()
    return row is not None


async def record_event_processed(
    *,
    razorpay_event_id: str,
    event_type: str,
    product: str,
    payload: dict[str, Any],
    session: Session,
    user_id: uuid.UUID | None = None,
    processing_result: dict[str, Any] | None = None,
) -> bool:
    """INSERT a ``payment_events`` row for the just-processed event.

    Returns True iff the INSERT succeeded; False when the UNIQUE constraint
    on ``razorpay_event_id`` fires (which means a concurrent worker beat
    us to it — treat as "already processed", no-op).

    Args:
        razorpay_event_id: The top-level ``id`` from the webhook body.
        event_type: The ``event`` field (e.g. ``"subscription.activated"``).
        product: ``"munshi"`` or ``"nowlez"`` — used by the
            ``payment_events_product_check`` CHECK constraint and by
            triage queries that filter per-brand.
        payload: The full webhook body (persisted as JSONB).
        session: Open SQLAlchemy session.
        user_id: Optional resolved user. Note: ``payment_events`` does
            not currently carry a user_id column (see plan §3 narrowing),
            so this argument is accepted for forward-compat but currently
            stored only in ``payload`` indirectly. Kept on the signature
            so we don't have to break callers later if/when the column
            is added.
        processing_result: Optional structured result of handler dispatch.
            Stored under ``payload['__processing_result__']`` so the
            webhook router's audit trail can be reconstructed later
            without altering the model.

    Returns:
        True on successful insert, False if the row already exists.
    """
    from data_access.models.billing import PaymentEvent

    if not razorpay_event_id:
        return False

    # Stash the processing_result inside the JSONB column without
    # creating a new column. We make a shallow copy so we don't mutate
    # the caller's dict.
    persisted_payload: dict[str, Any] = dict(payload)
    if processing_result is not None:
        persisted_payload["__processing_result__"] = processing_result
    # Same for user_id — preserved alongside the rest so triage can see it.
    if user_id is not None:
        persisted_payload["__user_id__"] = str(user_id)

    row = PaymentEvent(
        razorpay_event_id=razorpay_event_id,
        event_type=event_type,
        product=product,
        payload=persisted_payload,
        processed_at=datetime.now(timezone.utc),
    )
    # Wrap the INSERT in a SAVEPOINT so an IntegrityError from the
    # UNIQUE constraint only rolls back this nested transaction and
    # leaves the outer transaction (with the webhook handler's
    # side-effects) intact. Without the savepoint, ``session.rollback()``
    # discards every change made earlier in the request.
    nested = session.begin_nested()
    session.add(row)
    try:
        session.flush()
        nested.commit()
    except IntegrityError:
        # Concurrent worker won the race. The savepoint rollback
        # surgically removes the doomed INSERT without touching the
        # outer-transaction state.
        nested.rollback()
        return False
    return True


__all__ = [
    "is_event_already_processed",
    "record_event_processed",
]
