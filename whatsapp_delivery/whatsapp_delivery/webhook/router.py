"""Inbound message routing: STOP/Nowlez vs. Munshi default.

The router is intentionally a thin pure function over a parsed
``IncomingMessage`` plus a SQLAlchemy session. Its job is to answer the
question "which brand handler should process this message?" without
performing any side effects -- the caller takes the ``RouteTarget`` and
dispatches.

The module-level ``_get_user`` / ``_has_munshi`` / ``_has_nowlez`` aliases
are intentional: tests monkey-patch them per-case to avoid spinning up a DB
fixture for what is fundamentally a branching-logic check.
"""
from __future__ import annotations

from typing import Any

from data_access.daos.user_dao import (
    get_by_phone as _get_user,
    has_munshi_extension as _has_munshi,
    has_nowlez_extension as _has_nowlez,
)

from whatsapp_delivery.models import RouteTarget
from whatsapp_delivery.webhook.parser import IncomingMessage


# Keywords are uppercased + punctuation-stripped at compare time. Coverage is
# English (the most common case), romanized Hindi (the WhatsApp QWERTY input
# norm), and Devanagari (for users with a Hindi keyboard installed).
STOP_KEYWORDS: frozenset[str] = frozenset({
    # English
    "STOP", "STOP ALL", "STOP NOTIFICATIONS", "PAUSE", "UNSUBSCRIBE",
    "OPT OUT", "OPT-OUT", "CANCEL",
    # Romanized Hindi
    "BAND", "BAND KARO", "BAND KAR", "BANDH", "BANDH KARO",
    "CHHODO", "ROK", "ROK DO", "RUKO", "RUK",
    # Devanagari
    "बंद", "बंद करो", "बंद कर", "रुक", "रुको", "रोक", "रोक दो", "छोड़ो",
})


def is_stop_keyword(text: str | None) -> bool:
    """Return True iff ``text`` is one of the STOP-keyword phrases.

    The match strips trailing whitespace and uppercases ASCII before lookup.
    Trailing punctuation (``.``, ``!``) and a single trailing word are
    tolerated so things like ``STOP.``, ``stop!``, and ``STOP ALL`` all
    match the same opt-out path.

    False on None/empty, and on prefixes like ``stopover`` that contain a
    keyword but aren't one (the start-with check requires a word boundary).
    """
    if not text:
        return False
    normalized = text.strip().upper()
    if normalized in STOP_KEYWORDS:
        return True
    # Tolerate a single trailing punctuation char.
    if normalized.endswith((".", "!", "?")) and normalized[:-1] in STOP_KEYWORDS:
        return True
    # Tolerate a single trailing word ("STOP ALL"-style is already in the
    # keyword set, but generic "STOP <anything>" should also match -- guard
    # against multi-word phrases like "stop the war please" by limiting to
    # exactly one trailing word.
    for kw in STOP_KEYWORDS:
        if normalized.startswith(kw + " "):
            tail = normalized[len(kw) + 1:].strip()
            if " " not in tail:
                return True
        if normalized.startswith(kw + "!") or normalized.startswith(kw + "."):
            return True
    return False


def route_inbound(message: IncomingMessage, db_session: Any) -> RouteTarget:
    """Pure function: inbound message -> (brand, handler, user_id).

    Routing precedence:
      1. Unknown phone -> Munshi default (handles onboarding).
      2. STOP keyword AND user has Nowlez extension -> Nowlez ``stop`` handler.
      3. User has Munshi extension -> Munshi default (Munshi wins on overlap).
      4. Otherwise -> Nowlez ``unknown_nowlez`` (handler decides reply policy).
    """
    user = _get_user(db_session, message.from_phone)
    if user is None:
        return RouteTarget(brand="munshi", handler="default", user_id=None)

    has_munshi = _has_munshi(db_session, user.id)
    has_nowlez = _has_nowlez(db_session, user.id)

    if is_stop_keyword(message.text) and has_nowlez:
        return RouteTarget(brand="nowlez", handler="stop", user_id=user.id)

    if has_munshi:
        return RouteTarget(brand="munshi", handler="default", user_id=user.id)

    return RouteTarget(brand="nowlez", handler="unknown_nowlez", user_id=user.id)
