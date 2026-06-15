"""Unit tests for the media-header template filer's payload builder.

These exercise the pure ``build_components`` / ``build_create_body``
functions — no network, no httpx (it's imported lazily in the filer's
network functions, so importing the module here is cheap).
"""
import pytest

from whatsapp_delivery.tools.file_media_template import (
    build_components,
    build_create_body,
)


def _by_type(components, type_):
    return [c for c in components if c["type"] == type_]


def test_video_header_attaches_header_handle():
    comps = build_components(
        header_format="video",
        body_text="Hi {{1}}",
        body_examples=["Rahul"],
        header_handle="HANDLE123",
    )
    header = _by_type(comps, "HEADER")[0]
    assert header["format"] == "VIDEO"
    assert header["example"] == {"header_handle": ["HANDLE123"]}


def test_media_header_without_handle_omits_example():
    # dry-run preview path: no handle yet, so no example key (would be
    # filled after the resumable upload).
    comps = build_components(
        header_format="video", body_text="Hi", header_handle=None,
    )
    header = _by_type(comps, "HEADER")[0]
    assert "example" not in header


def test_body_examples_are_positional_rows():
    comps = build_components(
        header_format="video", body_text="Hi {{1}} {{2}}",
        body_examples=["Rahul", "Delhi"], header_handle="H",
    )
    body = _by_type(comps, "BODY")[0]
    assert body["example"] == {"body_text": [["Rahul", "Delhi"]]}


def test_body_without_examples_has_no_example_key():
    comps = build_components(header_format="text", header_text="Hello",
                             body_text="No vars here")
    body = _by_type(comps, "BODY")[0]
    assert "example" not in body


def test_text_header_carries_text_not_handle():
    comps = build_components(
        header_format="text", header_text="Welcome", body_text="Hi",
    )
    header = _by_type(comps, "HEADER")[0]
    assert header == {"type": "HEADER", "format": "TEXT", "text": "Welcome"}


def test_footer_rendered_when_present():
    comps = build_components(
        header_format="video", body_text="Hi", header_handle="H",
        footer_text="Reply STOP to unsubscribe.",
    )
    footer = _by_type(comps, "FOOTER")
    assert footer and footer[0]["text"] == "Reply STOP to unsubscribe."


def test_quick_reply_button():
    comps = build_components(
        header_format="video", body_text="Hi", header_handle="H",
        quick_reply="Get Started",
    )
    buttons = _by_type(comps, "BUTTONS")[0]["buttons"]
    assert buttons == [{"type": "QUICK_REPLY", "text": "Get Started"}]


def test_url_button():
    comps = build_components(
        header_format="video", body_text="Hi", header_handle="H",
        url_button=("Open", "https://nowlez.com/start"),
    )
    buttons = _by_type(comps, "BUTTONS")[0]["buttons"]
    assert buttons == [
        {"type": "URL", "text": "Open", "url": "https://nowlez.com/start"}
    ]


def test_rejects_two_buttons():
    with pytest.raises(ValueError):
        build_components(
            header_format="video", body_text="Hi", header_handle="H",
            quick_reply="A", url_button=("B", "https://x"),
        )


def test_rejects_unknown_header_format():
    with pytest.raises(ValueError):
        build_components(header_format="audio", body_text="Hi")


def test_create_body_uppercases_category():
    body = build_create_body(
        name="munshi_welcome_video_v1", category="marketing", language="en",
        components=[{"type": "BODY", "text": "Hi"}],
    )
    assert body == {
        "name": "munshi_welcome_video_v1",
        "category": "MARKETING",
        "language": "en",
        "components": [{"type": "BODY", "text": "Hi"}],
    }


def test_component_order_is_header_body_footer_buttons():
    comps = build_components(
        header_format="video", body_text="Hi {{1}}", body_examples=["X"],
        header_handle="H", footer_text="bye", quick_reply="Go",
    )
    assert [c["type"] for c in comps] == ["HEADER", "BODY", "FOOTER", "BUTTONS"]
