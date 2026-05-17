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
    """Disable both WhatsApp toggles + audit + send confirmation template."""
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

    await enqueue_send_template(
        to=message.from_phone,
        template_name="nowlez_stop_confirmation_v1",
        language="en_US",
        variables={},
        brand="nowlez",
    )
