"""Tests for whatsapp_delivery.meta_client.MetaClient.

Uses respx to mock the Meta Graph API at the httpx layer.
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from whatsapp_delivery.errors import (
    Meta24HourWindowExpired,
    MetaInvalidMessage,
    MetaTransientError,
)
from whatsapp_delivery.meta_client import MetaClient


def _make_client() -> MetaClient:
    return MetaClient(phone_number_id="111", access_token="tok")


@respx.mock
def test_send_text_returns_wamid():
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.abc"}]})
    )
    assert _make_client().send_text("+919999999999", "hello") == "wamid.abc"


@respx.mock
def test_send_text_5xx_raises_transient():
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(503, text="server error")
    )
    with pytest.raises(MetaTransientError):
        _make_client().send_text("+919999999999", "hello")


@respx.mock
def test_send_text_24h_window_raises():
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(
            400, json={"error": {"code": 131047, "message": "outside 24h"}}
        )
    )
    with pytest.raises(Meta24HourWindowExpired):
        _make_client().send_text("+919999999999", "hello")


@respx.mock
def test_send_text_other_4xx_raises_invalid():
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(
            400, json={"error": {"code": 12345, "message": "bad"}}
        )
    )
    with pytest.raises(MetaInvalidMessage):
        _make_client().send_text("+919999999999", "hello")


# ---------------------------------------------------------------------------
# send_interactive_buttons
# ---------------------------------------------------------------------------


@respx.mock
def test_send_interactive_buttons_returns_wamid_and_truncates():
    """3 buttons OK; >3 truncated to first 3; titles truncated to 20 chars."""
    import json

    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.btn"}]})
    )
    long_title = "x" * 50  # >20, must be truncated by the client.
    wamid = _make_client().send_interactive_buttons(
        "+919999999999",
        body="Pick one",
        buttons=[
            {"id": "a", "title": long_title},
            {"id": "b", "title": "short"},
            {"id": "c", "title": "third"},
            {"id": "d", "title": "this-fourth-is-dropped"},
        ],
    )
    assert wamid == "wamid.btn"

    body = json.loads(route.calls.last.request.content.decode())
    buttons = body["interactive"]["action"]["buttons"]
    assert len(buttons) == 3  # truncated to 3
    assert buttons[0]["reply"]["title"] == "x" * 20  # title truncated to 20
    assert buttons[0]["reply"]["id"] == "a"


# ---------------------------------------------------------------------------
# send_interactive_list
# ---------------------------------------------------------------------------


@respx.mock
def test_send_interactive_list_truncates_rows_and_titles():
    """11 rows truncated to 10; long titles/descriptions clipped."""
    import json

    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.list"}]})
    )
    rows = [
        {"id": f"id-{i}", "title": "T" * 50, "description": "D" * 100}
        for i in range(11)
    ]
    wamid = _make_client().send_interactive_list(
        "+919999999999",
        body="pick",
        button_label="Open",
        section_title="Choices",
        rows=rows,
    )
    assert wamid == "wamid.list"

    body = json.loads(route.calls.last.request.content.decode())
    sent_rows = body["interactive"]["action"]["sections"][0]["rows"]
    assert len(sent_rows) == 10  # truncated to 10
    assert sent_rows[0]["title"] == "T" * 24  # title truncated to 24
    assert sent_rows[0]["description"] == "D" * 72  # description truncated to 72


@respx.mock
def test_send_interactive_list_row_without_description_omits_field():
    """Rows that don't carry ``description`` must not emit an empty key."""
    import json

    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.list2"}]})
    )
    _make_client().send_interactive_list(
        "+919999999999",
        body="pick",
        button_label="Open",
        section_title="Choices",
        rows=[{"id": "a", "title": "Alpha"}],
    )

    body = json.loads(route.calls.last.request.content.decode())
    row = body["interactive"]["action"]["sections"][0]["rows"][0]
    assert "description" not in row


# ---------------------------------------------------------------------------
# download_media — two-step (metadata, then binary)
# ---------------------------------------------------------------------------


@respx.mock
def test_download_media_two_step_success():
    """Metadata envelope returns URL → second GET returns bytes."""
    respx.get("https://graph.facebook.com/v20.0/media-xyz").mock(
        return_value=Response(
            200,
            json={"url": "https://lookaside.fbsbx.com/file.jpg", "mime_type": "image/jpeg"},
        )
    )
    respx.get("https://lookaside.fbsbx.com/file.jpg").mock(
        return_value=Response(200, content=b"\xff\xd8\xff\xe0" + b"JPEG-DATA"),
    )
    payload, mime = _make_client().download_media("media-xyz")
    assert payload.startswith(b"\xff\xd8")
    assert mime == "image/jpeg"


@respx.mock
def test_download_media_envelope_missing_url_raises_invalid():
    """If Meta returns an envelope without 'url', surface an invalid-message
    error (not a generic exception — callers depend on the typed surface)."""
    respx.get("https://graph.facebook.com/v20.0/media-nourl").mock(
        return_value=Response(200, json={"mime_type": "image/jpeg"}),  # no 'url'
    )
    with pytest.raises(MetaInvalidMessage):
        _make_client().download_media("media-nourl")


@respx.mock
def test_download_media_metadata_5xx_raises_transient():
    """A 5xx on step 1 must surface as transient so RQ retries."""
    respx.get("https://graph.facebook.com/v20.0/media-meta5xx").mock(
        return_value=Response(503, text="server error")
    )
    with pytest.raises(MetaTransientError):
        _make_client().download_media("media-meta5xx")


@respx.mock
def test_download_media_binary_5xx_raises_transient():
    """A 5xx on step 2 (the CDN fetch) is also transient."""
    respx.get("https://graph.facebook.com/v20.0/media-binary5xx").mock(
        return_value=Response(
            200,
            json={"url": "https://lookaside.fbsbx.com/blob.jpg", "mime_type": "image/jpeg"},
        )
    )
    respx.get("https://lookaside.fbsbx.com/blob.jpg").mock(
        return_value=Response(502, text="bad gateway")
    )
    with pytest.raises(MetaTransientError):
        _make_client().download_media("media-binary5xx")


# ---------------------------------------------------------------------------
# _raise_for_status edge cases
# ---------------------------------------------------------------------------


@respx.mock
def test_send_text_4xx_non_json_response_raises_invalid():
    """If a 4xx comes back with a non-JSON body (e.g. HTML error page),
    we still raise MetaInvalidMessage rather than crash on json()."""
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(400, text="<html>nope</html>"),
    )
    with pytest.raises(MetaInvalidMessage):
        _make_client().send_text("+919999999999", "hello")


# ---------------------------------------------------------------------------
# upload_media + send_document
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_media_returns_id():
    respx.post("https://graph.facebook.com/v20.0/111/media").mock(
        return_value=Response(200, json={"id": "media-up-001"})
    )
    media_id = _make_client().upload_media(
        data=b"%PDF-1.4\n...", filename="doc.pdf", mime_type="application/pdf",
    )
    assert media_id == "media-up-001"


@respx.mock
def test_send_document_with_filename_and_caption():
    """Filename + caption land in the JSON body (and are length-clipped
    defensively against Meta's caps)."""
    import json

    route = respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.doc"}]})
    )
    long_filename = "x" * 500
    long_caption = "y" * 2000
    wamid = _make_client().send_document(
        "+919999999999",
        media_id="media-zzz",
        filename=long_filename,
        caption=long_caption,
    )
    assert wamid == "wamid.doc"

    body = json.loads(route.calls.last.request.content.decode())
    doc = body["document"]
    assert doc["id"] == "media-zzz"
    # Defensive truncation: Meta caps filename at 240 and caption at 1024.
    assert len(doc["filename"]) == 240
    assert len(doc["caption"]) == 1024


@respx.mock
def test_send_document_from_bytes_uploads_then_sends():
    """The convenience method does both an upload and a send."""
    respx.post("https://graph.facebook.com/v20.0/111/media").mock(
        return_value=Response(200, json={"id": "media-bytes-001"})
    )
    respx.post("https://graph.facebook.com/v20.0/111/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.bytes"}]})
    )
    wamid = _make_client().send_document_from_bytes(
        "+919999999999",
        data=b"%PDF-1.4\n...",
        filename="x.pdf",
        caption="cap",
    )
    assert wamid == "wamid.bytes"
