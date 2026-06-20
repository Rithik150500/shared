"""Integration: inbound STOP → router → handler → preferences disabled +
confirmation enqueued.

Per plan §19.2.1. The flow is:

  1. Webhook payload arrives with body=STOP from a Nowlez user.
  2. ``route_inbound`` returns ``RouteTarget(brand='nowlez', handler='stop')``.
  3. ``handle_stop_keyword`` runs:
     - Flips both ``users_nowlez`` consent flags to False.
     - Writes an ``audit_log`` row with ``event_type='whatsapp.opted_out_via_inbound'``.
     - Enqueues a ``nowlez_stop_confirmation_v2`` template send.

This skeleton walks the whole flow against an in-memory SQLite +
fakeredis. The Nowlez bridge POST (Munshi-side) is out of scope for the
shared library — that's covered by the Nowlez-side integration tests.
"""
from __future__ import annotations

import asyncio
import uuid

from data_access.daos import user_dao
from data_access.models import AuditLog, UserNowlez

from whatsapp_delivery.dispatch import queue as q
from whatsapp_delivery.models import RouteTarget
from whatsapp_delivery.webhook.parser import IncomingMessage
from whatsapp_delivery.webhook.router import route_inbound
from whatsapp_delivery.webhook.stop_handler import handle_stop_keyword


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _seed_nowlez_user(db_session, *, phone: str = "+919876543210"):
    """Create a shared User + Nowlez extension with both consent flags on."""
    user, _created = user_dao.get_or_create_by_phone(db_session, phone=phone)
    ext = UserNowlez(
        user_id=user.id,
        name="Asha",  # users_nowlez.name is NOT NULL
        whatsapp_events_enabled=True,
        whatsapp_reminders_enabled=True,
    )
    db_session.add(ext)
    db_session.commit()
    return user


def test_stop_keyword_full_flow(db_session, fake_redis):
    """Inbound STOP from a Nowlez user → flags off + audit + confirmation queued."""
    user = _seed_nowlez_user(db_session)

    msg = IncomingMessage(
        meta_message_id="wamid.stop-int",
        from_phone="+919876543210",
        timestamp=1715817600,
        type="text",
        text="STOP",
    )

    # 1. Router target.
    target = route_inbound(msg, db_session)
    assert target.brand == "nowlez"
    assert target.handler == "stop"
    assert str(target.user_id) == str(user.id)

    # 2. Handler side-effects.
    _run(handle_stop_keyword(user.id, msg, db_session))

    # Consent flags flipped to False.
    ext = (
        db_session.query(UserNowlez)
        .filter_by(user_id=user.id)
        .one()
    )
    assert ext.whatsapp_events_enabled is False
    assert ext.whatsapp_reminders_enabled is False

    # Audit log row written with the right event type.
    audit = (
        db_session.query(AuditLog)
        .filter_by(event_type="whatsapp.opted_out_via_inbound")
        .one()
    )
    assert str(audit.user_id) == str(user.id)

    # Confirmation template enqueued.
    from rq import Queue
    queued = Queue("whatsapp_send", connection=fake_redis).jobs
    assert any(
        j.kwargs.get("template_name") == "nowlez_stop_confirmation_v2"
        for j in queued
    )
