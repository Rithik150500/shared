"""Munshi grace + suspension state machine (spec Section 1.3).

A Munshi invoice progresses through:

    sent → in_grace (on the due date) → suspended (7 days past due)

and on the happy path:

    sent / in_grace → paid (webhook) → resumed (cases re-enabled if previously suspended)

Three operations live here:

* :func:`send_payment_reminder` — issues the D-5 / D-3 / D-0 reminder
  template and, when ``days_offset >= 0`` (on or past the due date),
  transitions ``status='sent' → 'in_grace'`` and sets
  ``grace_expires_at = due_at + 7 days``.
* :func:`suspend_user` — flips ``cases.refresh_enabled=False`` for every
  case the user owns, closes any open ``case_billing_periods``, marks
  the latest invoice ``status='suspended'``, sends
  ``munshi_suspension_activated_v1``, and writes an audit log entry.
  Crucially, it snapshots the case-ids that were active at suspension
  time into ``users_munshi.current_state['was_active_before_suspension']``
  so :func:`resume_user` knows which to bring back. (We use the
  existing JSONB column rather than adding a new ``cases`` column to
  avoid migration churn — coordinate with sub-project A if the column
  approach is preferred later.)
* :func:`resume_user` — restores ``refresh_enabled=True`` on the cases
  named in the snapshot, opens fresh ``case_billing_periods`` for them,
  and clears the snapshot. Idempotent: a second call is a no-op.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session


# Grace window after the due date before the user is suspended (spec
# Section 1.3: 7 days). Duplicated from BillingConfig for the same
# reason the price constant is duplicated in `invoices.py`.
MUNSHI_GRACE_PERIOD_DAYS: int = 7


# Reminder templates keyed by the *offset* of "today" relative to due_at
# in whole days. Positive offsets mean past-due. Negative mean upcoming.
_REMINDER_TEMPLATES: dict[int, str] = {
    -5: "munshi_payment_reminder_d5_v1",
    -3: "munshi_payment_reminder_d3_v1",
    0: "munshi_payment_reminder_d0_v1",
}


async def send_payment_reminder(
    invoice_id: uuid.UUID,
    days_offset: int,
    send_template_fn: Callable[..., Awaitable[Any]],
    session: Session,
) -> None:
    """Send the day-offset-specific reminder template for an invoice.

    Args:
        invoice_id: Primary key of the ``munshi_invoices`` row.
        days_offset: Days relative to ``due_at``. Negative = upcoming
            (D-5, D-3); 0 = due today (transitions to ``in_grace``).
        send_template_fn: Async callable matching the sub-project B
            ``enqueue_send_template`` signature.
        session: Open SQLAlchemy session.

    Status transitions:

    * ``days_offset < 0`` — status stays ``'sent'``.
    * ``days_offset >= 0`` — flips ``status='in_grace'`` and sets
      ``grace_expires_at = due_at + 7 days`` (idempotent — the flip is
      conditional on current status being ``'sent'``).
    """
    from data_access.models.billing import MunshiInvoice
    from data_access.models.user import User

    invoice = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.id == invoice_id)
    ).scalar_one_or_none()
    if invoice is None:
        return  # Best-effort — caller is responsible for not feeding stale ids.

    template_name = _REMINDER_TEMPLATES.get(days_offset)
    if template_name is None:
        # No template defined for this offset (e.g. D-1 reminders not
        # in the spec). Silently no-op so the cron can call freely.
        return

    if days_offset >= 0 and invoice.status == "sent":
        invoice.status = "in_grace"
        if invoice.due_at is not None:
            invoice.grace_expires_at = invoice.due_at + timedelta(
                days=MUNSHI_GRACE_PERIOD_DAYS
            )
        else:
            invoice.grace_expires_at = datetime.now(timezone.utc) + timedelta(
                days=MUNSHI_GRACE_PERIOD_DAYS
            )

    phone = session.execute(
        select(User.phone).where(User.id == invoice.user_id)
    ).scalar_one_or_none()
    if phone:
        await send_template_fn(
            to=phone,
            template=template_name,
            variables={
                "amount_rupees": invoice.amount_paise // 100,
                "due_date": (
                    invoice.due_at.date().isoformat() if invoice.due_at else ""
                ),
            },
            brand="munshi",
        )


async def suspend_user(
    user_id: uuid.UUID,
    session: Session,
    send_template_fn: Callable[..., Awaitable[Any]],
) -> None:
    """Suspend a user whose Munshi invoice has lapsed past the grace window.

    Side-effects (in order):

    1. Snapshot the set of currently-active case_ids into
       ``users_munshi.current_state['was_active_before_suspension']``
       so :func:`resume_user` can restore them.
    2. Set ``cases.refresh_enabled=False`` for every case the user owns
       whose refresh was currently enabled.
    3. Close any open ``case_billing_periods`` (set ``period_end=NOW()``).
    4. Mark the user's latest ``munshi_invoices`` row
       ``status='suspended'`` and write ``suspended_at=NOW()``.
    5. Send ``munshi_suspension_activated_v1`` template.
    6. Append to ``audit_log`` with ``event_type='munshi.user_suspended'``.
    """
    from data_access.models.audit import AuditLog
    from data_access.models.billing import CaseBillingPeriod, MunshiInvoice
    from data_access.models.case import Case
    from data_access.models.user import User, UserMunshi

    now = datetime.now(timezone.utc)

    # 1. Snapshot active cases for later resume. Sub-project A may
    # eventually move this to a `cases.was_active_before_suspension`
    # boolean column; for now we ride along on the existing JSONB.
    active_case_ids = session.execute(
        select(Case.id)
        .where(Case.user_id == user_id)
        .where(Case.refresh_enabled.is_(True))
    ).scalars().all()
    munshi_row = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one_or_none()
    if munshi_row is not None:
        # Coerce UUIDs to strings so the JSONB serializer is happy on
        # both SQLite (JSON) and Postgres (JSONB).
        state = dict(munshi_row.current_state or {})
        state["was_active_before_suspension"] = [
            str(cid) for cid in active_case_ids
        ]
        munshi_row.current_state = state

    # 2. Disable refresh for every case.
    session.execute(
        update(Case)
        .where(Case.user_id == user_id)
        .values(refresh_enabled=False)
    )

    # 3. Close active billing periods.
    session.execute(
        update(CaseBillingPeriod)
        .where(CaseBillingPeriod.user_id == user_id)
        .where(CaseBillingPeriod.period_end.is_(None))
        .values(period_end=now)
    )

    # 4. Mark latest invoice suspended.
    latest_invoice = session.execute(
        select(MunshiInvoice)
        .where(MunshiInvoice.user_id == user_id)
        .order_by(MunshiInvoice.cycle_end.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_invoice is not None:
        latest_invoice.status = "suspended"
        latest_invoice.suspended_at = now

    # 5. Send the suspension template.
    phone = session.execute(
        select(User.phone).where(User.id == user_id)
    ).scalar_one_or_none()
    if phone:
        await send_template_fn(
            to=phone,
            template="munshi_suspension_activated_v1",
            variables={},
            brand="munshi",
        )

    # 6. Audit log.
    session.add(
        AuditLog(
            event_type="munshi.user_suspended",
            user_id=user_id,
            source="munshi",
            metadata_={"cases_paused": len(active_case_ids)},
        )
    )
    session.flush()


async def resume_user(
    user_id: uuid.UUID,
    session: Session,
) -> None:
    """Restore a previously-suspended user (called on ``invoice.paid``).

    Reads the snapshot from
    ``users_munshi.current_state['was_active_before_suspension']``,
    re-enables ``refresh_enabled=True`` for those cases, opens fresh
    ``case_billing_periods`` for each, and clears the snapshot key.

    No template is sent here — :func:`mark_invoice_paid` sends the
    ``munshi_payment_received_v1`` template separately so the resume
    side-effect can be reused by admin-driven flows.

    Idempotent: if no snapshot exists, the function is a no-op.
    """
    from data_access.models.audit import AuditLog
    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.case import Case
    from data_access.models.user import UserMunshi

    munshi_row = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one_or_none()
    if munshi_row is None:
        return

    state = dict(munshi_row.current_state or {})
    snapshot = state.pop("was_active_before_suspension", None)
    munshi_row.current_state = state

    if not snapshot:
        return

    # Re-enable refresh for the snapshotted cases.
    case_uuids = [uuid.UUID(s) if isinstance(s, str) else s for s in snapshot]
    session.execute(
        update(Case)
        .where(Case.id.in_(case_uuids))
        .where(Case.user_id == user_id)
        .values(refresh_enabled=True)
    )

    # Open fresh billing periods. We don't dedup against existing open
    # rows because suspend_user just closed them all — but the check
    # below makes the function safe against out-of-order webhooks.
    now = datetime.now(timezone.utc)
    for cid in case_uuids:
        already_open = session.execute(
            select(CaseBillingPeriod.id)
            .where(CaseBillingPeriod.case_id == cid)
            .where(CaseBillingPeriod.period_end.is_(None))
            .limit(1)
        ).first()
        if already_open is not None:
            continue
        session.add(
            CaseBillingPeriod(
                user_id=user_id,
                case_id=cid,
                period_start=now,
                period_end=None,
            )
        )

    session.add(
        AuditLog(
            event_type="munshi.user_resumed",
            user_id=user_id,
            source="munshi",
            metadata_={"cases_restored": len(case_uuids)},
        )
    )
    session.flush()
