"""Tests for whatsapp_delivery.webhook.router — inbound STOP detection + brand routing.

Routing rules (per spec §5):
  - unknown phone (no shared user row) -> Munshi default
  - STOP-keyword AND user has a Nowlez extension -> Nowlez stop
  - has Munshi extension -> Munshi default (Munshi wins when both)
  - otherwise (Nowlez-only) -> Nowlez ``unknown_nowlez`` so the Nowlez handler
    decides whether to respond
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from whatsapp_delivery.webhook.parser import IncomingMessage
from whatsapp_delivery.webhook.router import (
    STOP_KEYWORDS,
    is_stop_keyword,
    route_inbound,
)


# --- is_stop_keyword ----------------------------------------------------------


@pytest.mark.parametrize("text", [
    "STOP", "stop", "Stop", "STOP ALL", "unsubscribe", "Pause", "CANCEL.",
    "band karo", "बंद करो", "रुक", "OPT-OUT", "stop!"
])
def test_is_stop_keyword_positive(text):
    assert is_stop_keyword(text) is True


@pytest.mark.parametrize("text", [
    None, "", "hello", "stopover", "stop the war please", "ban this user",
])
def test_is_stop_keyword_negative(text):
    assert is_stop_keyword(text) is False


def test_stop_keywords_includes_english_and_indic():
    # Sanity that the canonical set actually has the language coverage the plan
    # documents (English + romanized Hindi + Devanagari).
    assert "STOP" in STOP_KEYWORDS
    assert "BAND KARO" in STOP_KEYWORDS
    assert "बंद करो" in STOP_KEYWORDS


# --- route_inbound ------------------------------------------------------------


def _msg(text: str, phone: str = "+919999999999") -> IncomingMessage:
    return IncomingMessage(
        meta_message_id="wamid.x",
        from_phone=phone,
        timestamp=0,
        type="text",
        text=text,
    )


def test_routes_to_munshi_when_unknown_phone(monkeypatch):
    db = MagicMock()
    from whatsapp_delivery.webhook import router as r
    monkeypatch.setattr(r, "_get_user", lambda *_a, **_kw: None)
    target = route_inbound(_msg("hi"), db)
    assert target.brand == "munshi"
    assert target.handler == "default"
    assert target.user_id is None


def test_routes_stop_to_nowlez_for_dual_user(monkeypatch):
    db = MagicMock()
    from whatsapp_delivery.webhook import router as r
    user = MagicMock(id="u1")
    monkeypatch.setattr(r, "_get_user", lambda *_a, **_kw: user)
    monkeypatch.setattr(r, "_has_munshi", lambda *_a, **_kw: True)
    monkeypatch.setattr(r, "_has_nowlez", lambda *_a, **_kw: True)
    target = route_inbound(_msg("STOP"), db)
    assert target.brand == "nowlez"
    assert target.handler == "stop"
    assert target.user_id == "u1"


def test_routes_munshi_user_to_munshi_default(monkeypatch):
    db = MagicMock()
    from whatsapp_delivery.webhook import router as r
    user = MagicMock(id="u1")
    monkeypatch.setattr(r, "_get_user", lambda *_a, **_kw: user)
    monkeypatch.setattr(r, "_has_munshi", lambda *_a, **_kw: True)
    monkeypatch.setattr(r, "_has_nowlez", lambda *_a, **_kw: False)
    target = route_inbound(_msg("hello"), db)
    assert target.brand == "munshi"
    assert target.handler == "default"


def test_routes_nowlez_only_user_to_nowlez_unknown_handler(monkeypatch):
    db = MagicMock()
    from whatsapp_delivery.webhook import router as r
    user = MagicMock(id="u2")
    monkeypatch.setattr(r, "_get_user", lambda *_a, **_kw: user)
    monkeypatch.setattr(r, "_has_munshi", lambda *_a, **_kw: False)
    monkeypatch.setattr(r, "_has_nowlez", lambda *_a, **_kw: True)
    target = route_inbound(_msg("hello"), db)
    assert target.brand == "nowlez"
    assert target.handler == "unknown_nowlez"
    assert target.user_id == "u2"


def test_stop_for_munshi_only_user_does_not_route_to_nowlez_stop(monkeypatch):
    """STOP-keyword only short-circuits to Nowlez when the user actually has a
    Nowlez extension. A Munshi-only user typing STOP falls through to the
    Munshi default handler (Munshi has its own STOP flow inside the bot)."""
    db = MagicMock()
    from whatsapp_delivery.webhook import router as r
    user = MagicMock(id="u3")
    monkeypatch.setattr(r, "_get_user", lambda *_a, **_kw: user)
    monkeypatch.setattr(r, "_has_munshi", lambda *_a, **_kw: True)
    monkeypatch.setattr(r, "_has_nowlez", lambda *_a, **_kw: False)
    target = route_inbound(_msg("STOP"), db)
    assert target.brand == "munshi"
    assert target.handler == "default"
