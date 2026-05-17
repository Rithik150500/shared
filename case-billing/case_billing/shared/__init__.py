"""Cross-cutting helpers shared between Munshi and Nowlez flows.

Re-exports the unified webhook router + idempotency helpers so callers
can do ``from case_billing.shared import handle_unified_webhook`` rather
than reaching into the submodule paths.
"""

from case_billing.shared.idempotency import (
    is_event_already_processed,
    record_event_processed,
)
from case_billing.shared.webhook_router import (
    STATUS_ALREADY_PROCESSED,
    STATUS_DEFERRED,
    STATUS_HANDLER_ERROR,
    STATUS_PROCESSED,
    STATUS_UNHANDLED_EVENT,
    WebhookResult,
    handle_unified_webhook,
)

__all__ = [
    "WebhookResult",
    "handle_unified_webhook",
    "is_event_already_processed",
    "record_event_processed",
    "STATUS_PROCESSED",
    "STATUS_ALREADY_PROCESSED",
    "STATUS_UNHANDLED_EVENT",
    "STATUS_DEFERRED",
    "STATUS_HANDLER_ERROR",
]
