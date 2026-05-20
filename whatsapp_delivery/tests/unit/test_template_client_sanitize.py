"""D-6 variable interpolation sanitization tests.

The user-facing variable values flow straight into Meta's template-component
payload via ``str(v)``. Without sanitization, a user-supplied case title
containing a newline, control char, bidirectional-override codepoint, a
literal ``{{N}}`` placeholder, or a ten-thousand-character paste could
break Meta template validation or inject extra placeholders. The
``_sanitize_var`` helper makes the interpolation defensive.
"""
from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from whatsapp_delivery import template_client as tc
from whatsapp_delivery.template_client import TemplateClient, _sanitize_var


# ---------------------------------------------------------------------------
# _sanitize_var unit tests
# ---------------------------------------------------------------------------


def test_sanitize_strips_newlines_and_carriage_returns():
    """Control chars (incl. \\n, \\r, \\t implicit via 0x09) are stripped.

    Newline / carriage return outside of body content can break template
    rendering on the Meta side. Tab (0x09) is *kept* because templates
    sometimes legitimately use tab-aligned tables.
    """
    out = _sanitize_var("hello\nworld\rfoo")
    assert out == "helloworldfoo"


def test_sanitize_strips_null_and_other_control_bytes():
    raw = "a\x00b\x01c\x1fd\x7fe"
    out = _sanitize_var(raw)
    assert out == "abcde"


def test_sanitize_preserves_tabs_and_normal_unicode():
    """Tab (0x09) is in the gap range we deliberately keep; emoji + accents
    must pass through untouched."""
    out = _sanitize_var("hello\tworld — Mehta vs. State (न्याय) 🎉")
    assert "\t" in out
    assert "—" in out
    assert "न्याय" in out
    assert "🎉" in out


def test_sanitize_strips_rtl_and_bidi_override_chars():
    """RTL / LTR override codepoints can re-order the rendered string in
    surprising ways (a known phishing vector). Strip them all.
    """
    # ‮ is right-to-left override, ⁦ is LRI, ⁩ is PDI.
    raw = "case-id-‮malicious⁩-suffix"
    out = _sanitize_var(raw)
    assert "‮" not in out
    assert "⁩" not in out
    assert "case-id-malicious-suffix" == out


def test_sanitize_escapes_literal_braces_placeholder_pattern():
    """A user-supplied ``{{2}}`` must NOT round-trip back to the template as
    an additional placeholder slot — Meta interpolates ``{{N}}`` after the
    components are assembled.
    """
    out = _sanitize_var("name with {{2}} embedded")
    # Concrete output: literal braces kept but separated so Meta's
    # placeholder parser won't pick them up as a {{N}}.
    assert "{{2}}" not in out
    assert "2" in out


def test_sanitize_escapes_multiple_distinct_braces():
    out = _sanitize_var("uses {{1}} and {{42}} and {{100}}")
    assert "{{1}}" not in out
    assert "{{42}}" not in out
    assert "{{100}}" not in out


def test_sanitize_length_cap_truncates_with_ellipsis():
    """Strings beyond the cap are truncated with a trailing ellipsis so
    the recipient sees that data was elided rather than silent loss.
    """
    raw = "x" * 1000
    out = _sanitize_var(raw, max_len=20)
    assert len(out) == 20
    assert out.endswith("…")


def test_sanitize_length_cap_preserves_short_strings_unchanged():
    raw = "short"
    out = _sanitize_var(raw, max_len=256)
    assert out == "short"


def test_sanitize_normal_string_passes_through():
    """Vanilla ASCII case names round-trip exactly."""
    out = _sanitize_var("Smith vs. Bank — Order Dated 12-May-2026")
    assert out == "Smith vs. Bank — Order Dated 12-May-2026"


def test_sanitize_coerces_non_string_input():
    """``str(v)`` semantics preserved: integers, UUIDs, etc. become text."""
    import uuid
    u = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert _sanitize_var(u) == str(u)
    assert _sanitize_var(42) == "42"
    assert _sanitize_var(None) == "None"


# ---------------------------------------------------------------------------
# Integration: sanitization is applied at every send_template* interpolation
# site (lines that previously called str(v) without filtering).
# ---------------------------------------------------------------------------


_MESSAGES_URL = "https://graph.facebook.com/v20.0/111/messages"


def _client() -> TemplateClient:
    return TemplateClient(phone_number_id="111", access_token="tok")


def _captured_body(route) -> dict:
    return json.loads(route.calls.last.request.content.decode())


@respx.mock
def test_send_template_strips_control_chars_in_body_var():
    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "x"}]})
    )
    _client().send_template(
        to="+919876543210",
        name="welcome_v1",
        language="en_US",
        variables=["Asha\nKumar"],
    )
    params = _captured_body(route)["template"]["components"][0]["parameters"]
    assert params == [{"type": "text", "text": "AshaKumar"}]


@respx.mock
def test_send_template_with_document_strips_bidi():
    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "x"}]})
    )
    _client().send_template_with_document(
        to="+919876543210",
        name="order_judgment_v1",
        language="en_US",
        variables=["Mehta‮malicious⁩"],
        document_media_id="m-1",
    )
    body = _captured_body(route)
    body_params = body["template"]["components"][1]["parameters"]
    assert body_params[0]["text"] == "Mehtamalicious"


@respx.mock
def test_send_template_with_components_escapes_braces_in_body_var():
    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "x"}]})
    )
    _client().send_template_with_components(
        to="+919876543210",
        name="nowlez_new_order_v1",
        language="en_US",
        body_variables=["Mehta vs Bank {{2}}"],
        header_media_id="m-1",
        button_url_variables=["tok-x"],
    )
    body_params = _captured_body(route)["template"]["components"][1]["parameters"]
    # The user-supplied "{{2}}" must NOT survive intact.
    assert "{{2}}" not in body_params[0]["text"]


@respx.mock
def test_send_template_with_components_caps_button_url_var_length():
    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "x"}]})
    )
    huge = "x" * 1000
    _client().send_template_with_components(
        to="+919876543210",
        name="nowlez_new_order_v1",
        language="en_US",
        body_variables=["A"],
        button_url_variables=[huge],
    )
    # button is the 2nd component (body, button) because no header here.
    components = _captured_body(route)["template"]["components"]
    button_params = components[1]["parameters"]
    assert len(button_params[0]["text"]) <= 256


@respx.mock
def test_send_template_with_components_normal_string_passes_through():
    """Sanitization MUST NOT mangle a vanilla legitimate value."""
    route = respx.post(_MESSAGES_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "x"}]})
    )
    _client().send_template_with_components(
        to="+919876543210",
        name="nowlez_new_order_v1",
        language="en_US",
        body_variables=["Mehta vs. State"],
    )
    body_params = _captured_body(route)["template"]["components"][0]["parameters"]
    assert body_params == [{"type": "text", "text": "Mehta vs. State"}]
