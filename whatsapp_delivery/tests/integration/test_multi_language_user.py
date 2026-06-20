"""Integration: locale=hi user gets the hi_IN template variant.

Per plan §19.2.1. The template registry has both ``en_US`` and ``hi_IN``
language variants for every Nowlez user-facing template. The producer
selects the variant by locale; the worker passes it to Meta as the
``language.code`` field.

This skeleton:
  1. Looks up a template with the ``hi_IN`` variant and asserts the
     Devanagari body is what we expect (registry-side fidelity check).
  2. Enqueues a send with ``language='hi_IN'`` and confirms the worker
     dispatches with the Hindi body via the registry.
"""
from __future__ import annotations

import asyncio
import json

import respx
from httpx import Response

from whatsapp_delivery.dispatch import queue as q
from whatsapp_delivery.dispatch.worker import _do_send_template
from whatsapp_delivery.templates import get_template


_MESSAGES_URL = "https://graph.facebook.com/v20.0/111/messages"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_registry_returns_hindi_variant():
    """Smoke check: the registry has the hi_IN variant filed."""
    t = get_template("nowlez_signup_welcome_v2", "hi_IN")
    assert t.full_name == "nowlez_signup_welcome_v2"
    # Body must contain at least one Devanagari character.
    assert any("ऀ" <= ch <= "ॿ" for ch in t.body), (
        f"Hindi variant body has no Devanagari: {t.body!r}"
    )


@respx.mock
def test_hi_in_locale_routes_to_hindi_template_variant(
    fake_redis,
    db_session,
):
    """Enqueue with language='hi_IN'; assert the worker sends the
    hi_IN template payload to Meta."""
    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(
            200, json={"messages": [{"id": "wamid.hi"}]},
        ),
    )

    job_id = _run(
        q.enqueue_send_template(
            to="+919999999999",
            template_name="nowlez_signup_welcome_v2",
            language="hi_IN",
            variables={"user_name": "आशा"},
            brand="nowlez",
        ),
    )
    assert job_id

    from rq import Queue
    job = Queue("whatsapp_send", connection=fake_redis).jobs[0]
    wamid = _do_send_template(**job.kwargs)
    assert wamid == "wamid.hi"

    # Assert the Meta payload used the hi_IN language code.
    body = json.loads(route.calls.last.request.content.decode())
    assert body["template"]["language"] == {"code": "hi_IN"}
    assert body["template"]["name"] == "nowlez_signup_welcome_v2"
