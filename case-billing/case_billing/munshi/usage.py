"""DAO for the ``case_billing_periods`` table (spec Section 3.1).

A *billing period* row exists for every (user, case) pair while that case
is being tracked. ``period_start`` records when the case first became
billable; ``period_end`` is ``NULL`` while the case is still active and
gets set when the case is paused, deleted, or the user is suspended.

Three operations are exposed:

* :func:`open_billing_period` — INSERT a new active row for a case.
  Idempotent: a second call while an active row already exists is a
  no-op, so the case-save handler can call it without remembering
  whether it's already opened the period.
* :func:`close_billing_period` — UPDATE the latest active row's
  ``period_end`` so it falls out of future cycle counts. No-op when no
  active row exists (defensive — keeps the cron worker from raising on
  already-closed cases during retries).
* :func:`count_billable_cases_in_window` — return the COUNT(DISTINCT
  case_id) whose period overlaps the supplied cycle window. The 200-case
  cap from spec Section 1.2 is **not** enforced here; cap enforcement is
  the invoice generator's responsibility (so admins still see the true
  case count in dashboards). See :func:`generate_anniversary_invoice`.

All three are ``async def`` so they slot into the FastAPI / RQ async
call sites without adding executor hops. The underlying SQLAlchemy
session is the project's standard sync ``Session`` — no event-loop
blocking work happens here, only quick UPDATEs/SELECTs against a single
table.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session


async def open_billing_period(
    user_id: uuid.UUID,
    case_id: uuid.UUID,
    now: datetime,
    session: Session,
) -> None:
    """INSERT a new active billing period for ``(user_id, case_id)``.

    Idempotent: if a row already exists with ``period_end IS NULL`` for
    this case, the function returns without inserting.

    Args:
        user_id: Owner of the case.
        case_id: The case whose billing window is being opened.
        now: Timestamp (timezone-aware UTC) for ``period_start``.
        session: Open SQLAlchemy session.
    """
    from data_access.models.billing import CaseBillingPeriod

    active = session.execute(
        select(CaseBillingPeriod.id)
        .where(CaseBillingPeriod.case_id == case_id)
        .where(CaseBillingPeriod.period_end.is_(None))
        .limit(1)
    ).first()
    if active is not None:
        # Already open — no-op (per spec idempotency contract).
        return

    session.add(
        CaseBillingPeriod(
            user_id=user_id,
            case_id=case_id,
            period_start=now,
            period_end=None,
        )
    )


async def close_billing_period(
    user_id: uuid.UUID,
    case_id: uuid.UUID,
    now: datetime,
    session: Session,
) -> None:
    """UPDATE the latest active billing period for ``case_id`` to end now.

    Args:
        user_id: Required so we double-check we're not closing another
            tenant's row through a stale case_id.
        case_id: The case whose active billing window is being closed.
        now: Timestamp (timezone-aware UTC) to write into ``period_end``.
        session: Open SQLAlchemy session.

    No-op when no active row exists, so the cron's retry path stays
    side-effect-safe.
    """
    from data_access.models.billing import CaseBillingPeriod

    session.execute(
        update(CaseBillingPeriod)
        .where(CaseBillingPeriod.case_id == case_id)
        .where(CaseBillingPeriod.user_id == user_id)
        .where(CaseBillingPeriod.period_end.is_(None))
        .values(period_end=now)
    )


async def count_billable_cases_in_window(
    user_id: uuid.UUID,
    cycle_start: datetime,
    cycle_end: datetime,
    session: Session,
) -> int:
    """Return COUNT(DISTINCT case_id) whose period overlaps the cycle window.

    Overlap rule (matches the spec's ``tsrange ... && tsrange`` semantics
    but expressed in dialect-portable SQL):

        period_start < cycle_end AND (period_end IS NULL OR period_end > cycle_start)

    NULL ``period_end`` means "still open" — treated as +infinity for
    overlap, so an open case at the cycle close is counted.

    The 200-case cap from spec Section 1.2 is **not** applied here; the
    caller (:func:`generate_anniversary_invoice`) clamps the effective
    count to 200 paise-times when computing the invoice amount.
    """
    from data_access.models.billing import CaseBillingPeriod

    overlap = and_(
        CaseBillingPeriod.user_id == user_id,
        CaseBillingPeriod.period_start < cycle_end,
        or_(
            CaseBillingPeriod.period_end.is_(None),
            CaseBillingPeriod.period_end > cycle_start,
        ),
    )

    stmt = select(func.count(func.distinct(CaseBillingPeriod.case_id))).where(overlap)
    return int(session.execute(stmt).scalar_one())
