"""Inbound router stub -- filled in by Task 5.

Provides placeholder symbols so the ``whatsapp_delivery.webhook`` re-export block
imports cleanly while Task 3 is the only landed task. ``RouteTarget`` is a
plain dataclass with whatever fields we currently know; ``route_inbound`` and
``is_stop_keyword`` raise ``NotImplementedError`` until Task 5 lands.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteTarget:
    """Where an inbound message should be dispatched.

    Schema is finalized in Task 5; today this is a placeholder so
    ``from whatsapp_delivery.webhook import RouteTarget`` doesn't crash.
    """
    consumer: str = ""  # "munshi" | "nowlez"


def route_inbound(*args: object, **kwargs: object) -> RouteTarget:
    """Decide whether an inbound message goes to Munshi or Nowlez. (Task 5)"""
    raise NotImplementedError("route_inbound is implemented in Task 5")


def is_stop_keyword(*args: object, **kwargs: object) -> bool:
    """Return True if the incoming text matches a STOP-keyword pattern. (Task 5)"""
    raise NotImplementedError("is_stop_keyword is implemented in Task 5")
