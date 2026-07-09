"""STOP-keyword inbound handler.

Three side-effects per spec §5.3:
  1. flip both ``users_nowlez`` consent flags (events + reminders) to False
  2. write an ``audit_log`` row tagged ``whatsapp.opted_out_via_inbound``
  3. enqueue a confirmation template send (best-effort — failures don't undo
     the opt-out, but the user gets explicit confirmation when it lands)

The ``enqueue_send_template`` symbol is imported eagerly here so tests can
monkey-patch ``whatsapp_delivery.webhook.stop_handler.enqueue_send_template``
without having to reach into the dispatch package. The real RQ implementation
lives in ``whatsapp_delivery.dispatch.queue`` (Task 6).

D-10 ordering rationale.  Until the D-10 fix this handler committed the
consent revoke + audit row first, then awaited ``enqueue_send_template``.
If the producer crashed between commit and enqueue, the user was opted out
with no confirmation message — they think the bot is dead and have no
indication their STOP was honored. We now enqueue FIRST so a queue failure
short-circuits the handler before any DB mutation persists. Acceptable
failure mode: a successful enqueue followed by a commit failure leaves a
pending confirmation in the queue against a not-yet-revoked consent; the
user receives a "you've been opted out" reply, retries STOP, and the
handler re-processes cleanly. Strictly better than the prior silent-drop.
"""
from __future__ import annotations

import uuid
from typing import Any

from data_access.daos.audit_dao import log_event as _log_audit
from data_access.daos.whatsapp_dao import update_nowlez_preferences

from whatsapp_delivery.dispatch.queue import enqueue_send_template
from whatsapp_delivery.webhook.parser import IncomingMessage


async def handle_stop_keyword(
    user_id: uuid.UUID,
    message: IncomingMessage,
    db_session: Any,
) -> None:
    """Disable both WhatsApp toggles + audit + send confirmation template.

    Ordering (D-10): enqueue the confirmation send BEFORE any DB writes.
    A producer/queue failure must not leave consent revoked with no
    user-visible confirmation; the prior order (commit-then-enqueue)
    silently dropped the user when the await between commit and enqueue
    crashed. The DB writes are wrapped in a try/except so that any failure
    after the enqueue rolls back the in-flight transaction and re-raises.
    """
    # 1. Enqueue FIRST — no DB mutation has happened yet. If this raises,
    #    we exit immediately and the user's consent flags + audit log are
    #    unchanged. The user retries STOP later; the handler re-processes
    #    and they end up correctly opted-out.
    await enqueue_send_template(
        to=message.from_phone,
        template_name="nowlez_stop_confirmation_v1",
        language="en_US",
        variables={},
        brand="nowlez",
    )

    # 2. DB writes only after the enqueue succeeded. Wrap in try/except so
    #    any failure between the consent flip and the audit-row commit
    #    rolls the partial transaction back. (``update_nowlez_preferences``
    #    commits internally per its caller contract, so a failure inside
    #    ``_log_audit`` or its commit is the surviving window.)
    try:
        update_nowlez_preferences(
            db_session,
            user_id=user_id,
            whatsapp_events_enabled=False,
            whatsapp_reminders_enabled=False,
        )

        _log_audit(
            db_session,
            event_type="whatsapp.opted_out_via_inbound",
            user_id=user_id,
            source="nowlez",
            metadata={
                "meta_message_id": message.meta_message_id,
                # Cap inbound text at 100 chars so noisy payloads don't bloat the
                # audit log; the truncation is documented in the spec.
                "inbound_text": (message.text or "")[:100],
                "phone": message.from_phone,
            },
        )
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
