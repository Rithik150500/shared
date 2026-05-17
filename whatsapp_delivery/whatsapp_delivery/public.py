"""Aggregator re-export module.

``from whatsapp_delivery import ...`` is the canonical import path for
downstream callers (see :mod:`whatsapp_delivery.__init__`). This module
exists per the plan's File Structure as a second-class aggregator that
mirrors the same surface — useful for tooling that prefers an explicit
module to ``__init__`` (e.g. import-graph linters).

If a symbol is in :data:`__all__` here, it is part of the stable public
contract and removing it is a breaking change.
"""
from __future__ import annotations

# Imported here so the symbols are present on `whatsapp_delivery.public`
# the same way they are on `whatsapp_delivery`. Both paths point at the
# same underlying objects.
from whatsapp_delivery.config import WhatsAppConfig
from whatsapp_delivery.dispatch.queue import (
    enqueue_send_document,
    enqueue_send_template,
    enqueue_send_template_with_components,
    enqueue_send_text,
)
from whatsapp_delivery.dispatch.worker import process_send_queue
from whatsapp_delivery.errors import (
    Meta24HourWindowExpired,
    MetaInvalidMessage,
    MetaTransientError,
    RateLimitExceeded,
    ScaffoldError,
    StopRequested,
    TemplateNotFound,
    WebhookSignatureInvalid,
)
from whatsapp_delivery.idempotency import claim_message
from whatsapp_delivery.meta_client import MetaClient
from whatsapp_delivery.models import RouteTarget
from whatsapp_delivery.template_client import TemplateClient
from whatsapp_delivery.templates import (
    Template,
    TemplateAccessor,
    get_template,
    list_templates,
)
from whatsapp_delivery.webhook import (
    DeliveryStatus,
    IncomingButton,
    IncomingMedia,
    IncomingMessage,
    handle_stop_keyword,
    is_stop_keyword,
    parse_incoming,
    parse_status_updates,
    route_inbound,
    verify_signature,
)


__all__ = [
    # config
    "WhatsAppConfig",
    # dispatch
    "enqueue_send_document",
    "enqueue_send_template",
    "enqueue_send_template_with_components",
    "enqueue_send_text",
    "process_send_queue",
    # errors
    "Meta24HourWindowExpired",
    "MetaInvalidMessage",
    "MetaTransientError",
    "RateLimitExceeded",
    "ScaffoldError",
    "StopRequested",
    "TemplateNotFound",
    "WebhookSignatureInvalid",
    # idempotency
    "claim_message",
    # meta clients
    "MetaClient",
    "TemplateClient",
    # models
    "RouteTarget",
    # templates
    "Template",
    "TemplateAccessor",
    "get_template",
    "list_templates",
    # webhook
    "DeliveryStatus",
    "IncomingButton",
    "IncomingMedia",
    "IncomingMessage",
    "handle_stop_keyword",
    "is_stop_keyword",
    "parse_incoming",
    "parse_status_updates",
    "route_inbound",
    "verify_signature",
]
