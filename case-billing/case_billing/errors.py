"""Exception hierarchy for the case-billing package.

All billing exceptions derive from `BillingError` so callers can catch the
package's failure modes with a single `except` clause when desired.
"""

from __future__ import annotations


class BillingError(Exception):
    """Base class for all case-billing exceptions."""


class InvoiceNotFound(BillingError):
    """Raised when an invoice lookup (by id, period, or razorpay id) misses."""


class RazorpayApiError(BillingError):
    """Raised when the Razorpay HTTP API returns a non-2xx response.

    Carries the HTTP status_code and the (best-effort decoded) response body so
    upstream retry/alert logic can inspect both without re-parsing.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class WebhookSignatureInvalid(BillingError):
    """Raised when an inbound Razorpay webhook fails HMAC-SHA256 verification."""


class WebhookHandlerError(BillingError):
    """Raised when a per-event webhook handler fails.

    The unified webhook router surfaces this (instead of recording the event as
    processed) so the calling route returns a non-2xx and Razorpay retries —
    otherwise a transient handler failure would be permanently marked processed
    and never reconciled (a paid customer never activated)."""


class SubscriptionNotActive(BillingError):
    """Raised when an action requires an active subscription but the row is not."""


class TierAlreadySelected(BillingError):
    """Raised when a user attempts to pick a tier they have already chosen."""


class TrialAlreadyExpired(BillingError):
    """Raised when a trial-only action is attempted after the trial has lapsed."""
