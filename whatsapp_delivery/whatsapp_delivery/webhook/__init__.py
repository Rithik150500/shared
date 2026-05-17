"""Webhook subpackage: HMAC verify, payload parsing, inbound routing, STOP + status handling."""
from whatsapp_delivery.webhook.verifier import verify_signature
from whatsapp_delivery.webhook.parser import (
    DeliveryStatus,
    IncomingButton,
    IncomingMedia,
    IncomingMessage,
    parse_incoming,
    parse_status_updates,
)
from whatsapp_delivery.webhook.router import RouteTarget, route_inbound, is_stop_keyword
from whatsapp_delivery.webhook.status_handler import (
    apply_status_update,
    apply_status_updates,
)
from whatsapp_delivery.webhook.stop_handler import handle_stop_keyword

__all__ = [
    "DeliveryStatus",
    "IncomingButton",
    "IncomingMedia",
    "IncomingMessage",
    "RouteTarget",
    "apply_status_update",
    "apply_status_updates",
    "handle_stop_keyword",
    "is_stop_keyword",
    "parse_incoming",
    "parse_status_updates",
    "route_inbound",
    "verify_signature",
]
