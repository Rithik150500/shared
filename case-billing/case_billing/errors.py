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
    """Raised when the Razorpay HTTP API returns a non-2xx response."""


class WebhookSignatureInvalid(BillingError):
    """Raised when an inbound Razorpay webhook fails HMAC-SHA256 verification."""


class SubscriptionNotActive(BillingError):
    """Raised when an action requires an active subscription but the row is not."""


class TierAlreadySelected(BillingError):
    """Raised when a user attempts to pick a tier they have already chosen."""


class TrialAlreadyExpired(BillingError):
    """Raised when a trial-only action is attempted after the trial has lapsed."""
