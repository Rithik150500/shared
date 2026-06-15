"""Apply Meta delivery-status receipts to ``whatsapp_delivery_log``.

Meta delivers per-recipient receipts on the ``messages`` webhook field under
``value.statuses`` (sent / delivered / read / failed). ``parse_status_updates``
(see :mod:`whatsapp_delivery.webhook.parser`) returns one ``DeliveryStatus``
per receipt; this module is the side-effecting half — it looks up the log row
by Meta's ``wamid`` and stamps the new status + timestamp + (on failure) the
failure reason.

Sentry tagging on failure is best-effort: if the Sentry SDK isn't installed
(local dev / unit-test path) we just skip the capture. The DB write is the
load-bearing side-effect and runs in either case.

Spec reference: §4.7 (delivery-status pipeline).
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy.orm import Session

from data_access.daos.whatsapp_dao import update_delivery_status

from whatsapp_delivery.webhook.parser import DeliveryStatus

log = logging.getLogger(__name__)


def apply_status_update(session: Session, status: DeliveryStatus) -> int:
    """Persist one ``DeliveryStatus`` to the matching delivery-log row.

    Returns the number of rows updated (0 if no matching ``meta_message_id``
    exists yet — that can happen if the receipt races ahead of the worker's
    wamid-bind step; the row will be backfilled when the next receipt arrives
    or the wamid finally lands, since each receipt is idempotent for a given
    ``meta_message_id``).

    On ``status == 'failed'`` we additionally fire a Sentry breadcrumb so
    operators can spot per-template failure spikes. The import is lazy +
    swallowed because Sentry is optional in the dev / unit-test path.
    """
    rowcount = update_delivery_status(
        session,
        meta_message_id=status.meta_message_id,
        status=status.status,
        timestamp=status.timestamp,
        failure_reason=status.failure_reason,
    )
    if status.status == "failed":
        _report_failure(status)

    # Route to the broadcast ledger (no-op if wamid belongs to a non-broadcast
    # message or if the broadcast tables don't exist in this environment).
    try:
        from data_access.daos import broadcast_dao
        n = broadcast_dao.apply_broadcast_status(
            session,
            wamid=status.meta_message_id,
            status=status.status,
            error_code=status.error_code,
            failure_reason=status.failure_reason,
        )
        if n and status.error_code == 131026:   # undeliverable / not on WhatsApp
            row = broadcast_dao.get_by_wamid(session, status.meta_message_id)
            if row:
                broadcast_dao.suppress(
                    session,
                    wa_digits=row.wa_digits,
                    reason="undeliverable",
                    source="status_131026",
                )
    except Exception:
        log.warning(
            "broadcast ledger update failed for wamid=%s — degrading gracefully",
            status.meta_message_id,
            exc_info=True,
        )

    return rowcount


def apply_status_updates(
    session: Session, statuses: Iterable[DeliveryStatus],
) -> int:
    """Apply a batch of receipts, returning the total updated row count.

    Convenience wrapper for the webhook handler that gets a list from
    ``parse_status_updates``. Each receipt is applied independently so a
    single unmatched ``meta_message_id`` doesn't drop the rest.
    """
    total = 0
    for s in statuses:
        total += apply_status_update(session, s)
    return total


def _report_failure(status: DeliveryStatus) -> None:
    """Best-effort Sentry tag for failed sends. Swallows ImportError."""
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover — sentry is optional
        return
    sentry_sdk.capture_message(
        f"whatsapp_delivery.failed: {status.failure_reason or 'unknown'}",
        level="warning",
    )
