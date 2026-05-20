"""D-4 producer-side idempotency tests.

Covers the worker's ``dedup_key`` Redis-based short-circuit for
transactional templates (signup welcome, OTP, order-uploaded, stop
confirmation). Unlike B-3's daily-cadence dedup (DB-backed, per-IST-day),
D-4's dedup is per-arbitrary-key with a 10-minute Redis SETNX TTL —
intended to make RQ retries idempotent when the producer can supply a
stable dedup key.

For each ``_do_send_*`` flavor:

1. First call with a fresh ``dedup_key`` → Meta API hit + wamid returned.
2. Second call with same ``dedup_key`` (e.g. RQ retry) → short-circuits,
   no Meta call, returns "".
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

import fakeredis

from whatsapp_delivery.dispatch import worker as w


@pytest.fixture(autouse=True)
def _meta_env(monkeypatch):
    monkeypatch.setenv("META_PHONE_NUMBER_ID", "111")
    monkeypatch.setenv("META_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("META_VERIFY_TOKEN", "ver")
    monkeypatch.setenv("META_APP_SECRET", "sec")
    monkeypatch.delenv("WHATSAPP_NOWLEZ_DISABLED", raising=False)


@pytest.fixture
def fake_redis(monkeypatch):
    """In-memory Redis for the worker's send_dedup keys.

    The worker imports its own ``_get_redis`` (mirroring queue.py's
    pattern) so tests can patch the symbol without touching env vars.
    """
    fr = fakeredis.FakeRedis()
    monkeypatch.setattr(w, "_get_redis", lambda: fr)
    return fr


# ---------------------------------------------------------------------------
# _do_send_text
# ---------------------------------------------------------------------------


@respx.mock
def test_send_text_first_call_with_dedup_key_sends(fake_redis):
    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.txt1"}]})
    )
    wamid = w._do_send_text(
        to="+919999999999",
        body="hi",
        brand="munshi",
        user_id="u-1",
        dedup_key="signup-otp-abc",
    )
    assert wamid == "wamid.txt1"
    assert route.call_count == 1
    # Redis key is set with the documented prefix.
    assert fake_redis.exists("send_dedup:signup-otp-abc") == 1


@respx.mock
def test_send_text_second_call_with_same_dedup_key_short_circuits(fake_redis):
    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.txt2"}]})
    )
    common = dict(
        to="+919999999999",
        body="hi",
        brand="munshi",
        user_id="u-1",
        dedup_key="signup-otp-xyz",
    )
    first = w._do_send_text(**common)
    second = w._do_send_text(**common)
    assert first == "wamid.txt2"
    assert second == "", "second call must short-circuit"
    assert route.call_count == 1, "Meta must NOT be called twice"


# ---------------------------------------------------------------------------
# _do_send_template
# ---------------------------------------------------------------------------


@respx.mock
def test_send_template_first_call_with_dedup_key_sends(fake_redis):
    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.tmpl1"}]})
    )
    wamid = w._do_send_template(
        to="+919999999999",
        template_name="nowlez_signup_welcome_v1",
        language="en_US",
        variables={"user_name": "Asha"},
        brand="nowlez",
        media_bytes=None,
        media_filename=None,
        media_mime="application/pdf",
        related_case_id=None,
        related_order_id=None,
        user_id="u-1",
        dedup_per_day=False,
        dedup_key="welcome-u-1",
    )
    assert wamid == "wamid.tmpl1"
    assert route.call_count == 1
    assert fake_redis.exists("send_dedup:welcome-u-1") == 1


@respx.mock
def test_send_template_second_call_with_same_dedup_key_short_circuits(fake_redis):
    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.tmpl2"}]})
    )
    common = dict(
        to="+919999999999",
        template_name="nowlez_signup_welcome_v1",
        language="en_US",
        variables={"user_name": "Asha"},
        brand="nowlez",
        media_bytes=None,
        media_filename=None,
        media_mime="application/pdf",
        related_case_id=None,
        related_order_id=None,
        user_id="u-1",
        dedup_per_day=False,
        dedup_key="welcome-u-2",
    )
    first = w._do_send_template(**common)
    second = w._do_send_template(**common)
    assert first == "wamid.tmpl2"
    assert second == "", "RQ retry must short-circuit"
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# _do_send_template_with_components
# ---------------------------------------------------------------------------


@respx.mock
def test_send_template_with_components_first_call_with_dedup_key_sends(fake_redis):
    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.cmp1"}]})
    )
    wamid = w._do_send_template_with_components(
        to="+919999999999",
        template_name="nowlez_signup_welcome_v1",
        language="en_US",
        body_variables=["Asha"],
        brand="nowlez",
        header_media_id=None,
        button_url_variables=None,
        user_id="u-1",
        dedup_per_day=False,
        dedup_key="welcome-cmp-1",
    )
    assert wamid == "wamid.cmp1"
    assert route.call_count == 1
    assert fake_redis.exists("send_dedup:welcome-cmp-1") == 1


@respx.mock
def test_send_template_with_components_second_call_short_circuits(fake_redis):
    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.cmp2"}]})
    )
    common = dict(
        to="+919999999999",
        template_name="nowlez_signup_welcome_v1",
        language="en_US",
        body_variables=["Asha"],
        brand="nowlez",
        header_media_id=None,
        button_url_variables=None,
        user_id="u-1",
        dedup_per_day=False,
        dedup_key="welcome-cmp-2",
    )
    first = w._do_send_template_with_components(**common)
    second = w._do_send_template_with_components(**common)
    assert first == "wamid.cmp2"
    assert second == ""
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# _do_send_document
# ---------------------------------------------------------------------------


@respx.mock
def test_send_document_first_call_with_dedup_key_sends(fake_redis):
    respx.post("https://graph.facebook.com/v20.0/111/media").mock(
        return_value=Response(200, json={"id": "media-abc"})
    )
    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.doc1"}]})
    )
    wamid = w._do_send_document(
        to="+919999999999",
        document_bytes=b"%PDF-1.4\n...",
        caption="cap",
        filename="x.pdf",
        brand="munshi",
        dedup_key="doc-key-1",
    )
    assert wamid == "wamid.doc1"
    assert route.call_count == 1
    assert fake_redis.exists("send_dedup:doc-key-1") == 1


@respx.mock
def test_send_document_second_call_with_same_dedup_key_short_circuits(fake_redis):
    media_route = respx.post("https://graph.facebook.com/v20.0/111/media").mock(
        return_value=Response(200, json={"id": "media-abc"})
    )
    msg_route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.doc2"}]})
    )
    common = dict(
        to="+919999999999",
        document_bytes=b"%PDF-1.4\n...",
        caption="cap",
        filename="x.pdf",
        brand="munshi",
        dedup_key="doc-key-2",
    )
    first = w._do_send_document(**common)
    second = w._do_send_document(**common)
    assert first == "wamid.doc2"
    assert second == ""
    # Neither media upload nor message must be re-issued.
    assert media_route.call_count == 1
    assert msg_route.call_count == 1


# ---------------------------------------------------------------------------
# Cross-cutting: dedup_key=None / unset preserves existing behavior
# ---------------------------------------------------------------------------


@respx.mock
def test_dedup_key_unset_does_not_touch_redis(fake_redis):
    """Without an explicit dedup_key, send proceeds and Redis is untouched.

    Guards against an accidental change that would force all sends through
    a dedup lookup (which would change semantics for unaware callers).
    """
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.x"}]})
    )
    w._do_send_text(to="+919999999999", body="hi", brand="munshi", user_id=None)
    # Nothing should be written to the dedup keyspace.
    assert len(fake_redis.keys("send_dedup:*")) == 0


@respx.mock
def test_dedup_key_uses_different_keyspace_from_b3_daily(fake_redis):
    """D-4 uses ``send_dedup:`` prefix, NOT B-3's daily-claim DB key.

    A regression here would let one of the systems silently override the
    other.
    """
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.y"}]})
    )
    w._do_send_text(
        to="+919999999999",
        body="hi",
        brand="munshi",
        user_id="u-1",
        dedup_key="some-key",
    )
    keys = list(fake_redis.keys("*"))
    # We expect exactly one key — and it must be the send_dedup prefix.
    assert keys == [b"send_dedup:some-key"]
