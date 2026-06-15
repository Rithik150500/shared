"""Tests for video-header support in send_template_with_components.

Covers the ``munshi_welcome_video_v1`` marketing template which has a VIDEO
header (not a document header). The existing document path must be unaffected.
"""
from unittest.mock import patch
import pytest
from whatsapp_delivery.template_client import TemplateClient


def _capture():
    captured = {}
    def fake_post(self, body, *, what, timeout_seconds=None):
        captured["body"] = body
        return "wamid.TEST"
    return captured, fake_post


def test_video_header_emits_video_component():
    captured, fake_post = _capture()
    with patch.object(TemplateClient, "_post", fake_post):
        c = TemplateClient(phone_number_id="PNID", access_token="TOK")
        wamid = c.send_template_with_components(
            to="919643460175", name="munshi_welcome_video_v1", language="en",
            body_variables=["Rahul"], header_video_id="MEDIA123",
        )
    assert wamid == "wamid.TEST"
    comps = captured["body"]["template"]["components"]
    header = [x for x in comps if x["type"] == "header"][0]
    assert header["parameters"] == [{"type": "video", "video": {"id": "MEDIA123"}}]
    body = [x for x in comps if x["type"] == "body"][0]
    assert body["parameters"] == [{"type": "text", "text": "Rahul"}]


def test_document_and_video_header_are_mutually_exclusive():
    c = TemplateClient(phone_number_id="P", access_token="T")
    with pytest.raises(ValueError):
        c.send_template_with_components(
            to="9", name="n", language="en", body_variables=[],
            header_media_id="DOC", header_video_id="VID",
        )
