"""Shared dataclasses used across whatsapp_delivery.

Kept thin on purpose -- ORM models live in ``data_access.models``; this
module is for transport/routing dataclasses that the dispatch + webhook
layers pass around without touching the DB.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

Brand = Literal["munshi", "nowlez"]
Handler = Literal["default", "stop", "unknown_nowlez"]


@dataclass(frozen=True)
class RouteTarget:
    """Where an inbound message should be dispatched.

    Returned by ``whatsapp_delivery.webhook.router.route_inbound``. The brand
    decides which handler module loads; ``handler`` further narrows within
    that brand (``default`` -> normal flow; ``stop`` -> the STOP-keyword
    opt-out handler; ``unknown_nowlez`` -> Nowlez user who said something
    that isn't STOP, so Nowlez decides whether to echo / ignore / DM).
    """

    brand: Brand
    handler: Handler
    user_id: uuid.UUID | None
