"""Integration: free-text outside the 24h customer-service window returns 131047.

Per plan §19.2.1. Meta enforces a 24-hour window: any free-text message
to a user we haven't heard from in the last 24h is rejected with error
code 131047. Templates bypass this window — that's the whole point of
the registry — but free-text and ``send_text`` are gated.

This skeleton mocks Meta returning the 131047 error and asserts our
worker surfaces it as ``Meta24HourWindowExpired`` (which the producer
treats as a dead-letter, NOT a retry — RQ would loop forever).
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from whatsapp_delivery.dispatch.worker import _do_send_text
from whatsapp_delivery.errors import Meta24HourWindowExpired


_MESSAGES_URL = "https://graph.facebook.com/v20.0/111/messages"


@respx.mock
def test_free_text_outside_24h_raises_window_expired():
    """When Meta returns code 131047, the worker raises the typed error."""
    respx.post(_MESSAGES_URL).mock(
        return_value=Response(
            400,
            json={
                "error": {
                    "code": 131047,
                    "message": "Re-engagement message",
                }
            },
        ),
    )
    with pytest.raises(Meta24HourWindowExpired):
        _do_send_text(
            to="+919999999999",
            body="hello after silence",
            brand="munshi",
            user_id=None,
        )


@respx.mock
def test_24h_error_on_template_path_also_surfaces(caplog):
    """Templates SHOULD bypass the window, so 131047 on a template send
    is a configuration bug (template not approved, wrong name). The
    worker dead-letters + re-raises so ops are alerted."""
    from whatsapp_delivery.dispatch.worker import _do_send_template

    respx.post(_MESSAGES_URL).mock(
        return_value=Response(
            400,
            json={"error": {"code": 131047, "message": "outside 24h"}},
        ),
    )
    with pytest.raises(Meta24HourWindowExpired):
        _do_send_template(
            to="+919999999999",
            template_name="nowlez_signup_welcome_v2",
            language="en_US",
            variables={"user_name": "Asha"},
            brand="nowlez",
            media_bytes=None,
            media_filename=None,
            media_mime="application/pdf",
            related_case_id=None,
            related_order_id=None,
            user_id="u-1",
        )
    # Dead-letter log line for ops.
    assert any("dead-letter" in r.message for r in caplog.records)
