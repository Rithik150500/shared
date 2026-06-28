"""Consent/suppression gate at the worker (incident 2026-06-25 spam flag).

Every business-initiated *template* send flows through the worker. A user
who has opted out (a ``wa_suppression`` row, written by the inbound STOP
handler) must never receive another business-initiated template, regardless
of which producer (broadcast, cron, re-engagement) enqueued it. This is the
single chokepoint that guarantees "global STOP across all categories".

Two carve-outs, both tested here:
  1. The opt-out *confirmation* template is exempt — a user who just sent
     STOP must still learn the opt-out worked.
  2. Free-text replies (``_do_send_text``) are NOT gated: they are only
     deliverable inside Meta's 24h customer-service window (the user
     messaged first → implicit consent), so suppressing them would break
     the conversational bot. Only template paths are gated.
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from whatsapp_delivery.dispatch import worker as w


@pytest.fixture(autouse=True)
def _meta_env(monkeypatch):
    monkeypatch.setenv("META_PHONE_NUMBER_ID", "111")
    monkeypatch.setenv("META_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("META_VERIFY_TOKEN", "ver")
    monkeypatch.setenv("META_APP_SECRET", "sec")
    monkeypatch.delenv("WHATSAPP_NOWLEZ_DISABLED", raising=False)


# ---------------------------------------------------------------------------
# _is_suppressed_recipient — pure decision logic (no Meta, no real DB)
# ---------------------------------------------------------------------------


def test_opt_out_confirmation_template_is_exempt(monkeypatch):
    """The opt-out confirmation must bypass the gate WITHOUT a DB lookup."""
    def _boom(_digits):  # must not be reached for exempt templates
        raise AssertionError("_lookup_suppression must not run for exempt templates")

    monkeypatch.setattr(w, "_lookup_suppression", _boom)
    assert (
        w._is_suppressed_recipient(
            "+919999999999", template_name="nowlez_stop_confirmation_v2"
        )
        is False
    )


def test_suppressed_non_exempt_recipient_is_blocked(monkeypatch):
    monkeypatch.setattr(w, "_lookup_suppression", lambda digits: True)
    assert (
        w._is_suppressed_recipient(
            "+919999999999", template_name="munshi_welcome_video_v1"
        )
        is True
    )


def test_non_suppressed_recipient_is_allowed(monkeypatch):
    monkeypatch.setattr(w, "_lookup_suppression", lambda digits: False)
    assert (
        w._is_suppressed_recipient(
            "+919999999999", template_name="munshi_welcome_video_v1"
        )
        is False
    )


def test_blank_phone_is_not_suppressed(monkeypatch):
    monkeypatch.setattr(
        w, "_lookup_suppression",
        lambda digits: (_ for _ in ()).throw(AssertionError("no digits → no lookup")),
    )
    assert w._is_suppressed_recipient("", template_name="x") is False


def test_lookup_suppression_normalizes_to_digits(monkeypatch):
    """The DAO is keyed by digits-only wa_digits; '+91 999...' must normalize."""
    seen = {}
    monkeypatch.setattr(w, "_lookup_suppression", lambda digits: seen.setdefault("d", digits) or False)
    w._is_suppressed_recipient("+91 99999-99999", template_name="x")
    assert seen["d"] == "919999999999"


# ---------------------------------------------------------------------------
# Gate integration — suppressed recipient short-circuits before Meta
# ---------------------------------------------------------------------------


@respx.mock
def test_do_send_template_short_circuits_for_suppressed(monkeypatch):
    monkeypatch.setattr(w, "_is_suppressed_recipient", lambda to, template_name=None: True)
    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.nope"}]})
    )
    out = w._do_send_template(
        to="+919999999999",
        template_name="munshi_welcome_video_v1",
        language="en",
        variables={},
        brand="munshi",
        user_id="u-1",
    )
    assert out == ""
    assert route.call_count == 0, "Meta must NOT be called for an opted-out recipient"


@respx.mock
def test_do_send_template_with_components_short_circuits_for_suppressed(monkeypatch):
    monkeypatch.setattr(w, "_is_suppressed_recipient", lambda to, template_name=None: True)
    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.nope"}]})
    )
    out = w._do_send_template_with_components(
        to="+919999999999",
        template_name="munshi_welcome_video_v1",
        language="en",
        body_variables=[],
        brand="munshi",
        user_id="u-1",
    )
    assert out == ""
    assert route.call_count == 0


@respx.mock
def test_do_send_template_proceeds_for_non_suppressed(monkeypatch):
    """Regression guard: a non-suppressed recipient still sends normally."""
    monkeypatch.setattr(w, "_is_suppressed_recipient", lambda to, template_name=None: False)
    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.ok"}]})
    )
    out = w._do_send_template(
        to="+919999999999",
        template_name="nowlez_signup_welcome_v2",
        language="en_US",
        variables={"user_name": "Asha"},
        brand="nowlez",
        user_id="u-1",
    )
    assert out == "wamid.ok"
    assert route.call_count == 1


@respx.mock
def test_free_text_is_not_gated_by_suppression(monkeypatch):
    """`_do_send_text` is a 24h-window reply; suppression must NOT block it."""
    monkeypatch.setattr(
        w, "_is_suppressed_recipient",
        lambda to, template_name=None: (_ for _ in ()).throw(
            AssertionError("free-text must not consult the suppression gate")
        ),
    )
    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.txt"}]})
    )
    out = w._do_send_text(to="+919999999999", body="hi", brand="munshi")
    assert out == "wamid.txt"
    assert route.call_count == 1
