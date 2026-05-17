"""whatsapp_delivery — Meta WhatsApp Cloud API delivery for Nowlez and Munshi.

Public API: any symbol re-exported here is part of the stable contract that
downstream callers (Nowlez backend, Munshi bot_scaffold) depend on. The same
surface is also available via :mod:`whatsapp_delivery.public` for tools that
prefer importing from an explicit submodule.

Stability note: removing a name from ``__all__`` is a breaking change. If
you need to retire something, deprecate first (keep the re-export but add
a ``DeprecationWarning`` in the underlying definition), then remove in a
later major.
"""
__version__ = "0.1.0"

try:
    import sentry_sdk

    sentry_sdk.set_tag("package", "whatsapp_delivery")
except ImportError:  # pragma: no cover — Sentry is optional
    pass


# ---------------------------------------------------------------------------
# Re-exports
#
# Order: config first (everything downstream reads env via WhatsAppConfig),
# then errors (callers want to import these without pulling in heavier deps),
# then models, then the meta + template clients, then the templates
# registry, then the dispatch helpers, and finally the webhook layer. Lazy
# imports are avoided here — the package is small and downstream consumers
# expect every symbol to be available after `import whatsapp_delivery`.
# ---------------------------------------------------------------------------
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
    apply_status_update,
    apply_status_updates,
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
    "apply_status_update",
    "apply_status_updates",
    "handle_stop_keyword",
    "is_stop_keyword",
    "parse_incoming",
    "parse_status_updates",
    "route_inbound",
    "verify_signature",
]
