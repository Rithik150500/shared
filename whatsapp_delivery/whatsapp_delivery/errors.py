"""Scaffold error taxonomy.

Distinct from ecourts_client errors: these signal failures in the bot infrastructure
itself (Meta API, rate limits, internal misconfiguration) rather than in the eCourts
data layer.
"""
from __future__ import annotations


class ScaffoldError(Exception):
    """Base class for all bot-scaffold errors."""


class MetaTransientError(ScaffoldError):
    """Meta returned 5xx, 429, or a documented retry-able error code. Caller should retry.

    ``retry_after_seconds`` carries the value of the ``Retry-After`` HTTP header
    if Meta sent one (typical on 429 throttling). Callers (RQ retry policy)
    may honor it when scheduling the next attempt; ``None`` means Meta did
    not specify a delay.
    """

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class MetaInvalidMessage(ScaffoldError):
    """Meta returned 4xx that isn't a 24h-window expiry. Caller should NOT retry."""


class Meta24HourWindowExpired(ScaffoldError):
    """Meta error code 131047 -- the user hasn't messaged us in 24h, so we can only
    respond with a pre-approved template."""


class RateLimitExceeded(ScaffoldError):
    """Per-user / per-action rate limit hit. Caller should report to user, not retry."""


class TemplateNotFound(ScaffoldError):
    """Requested template name not in the registry."""


class StopRequested(ScaffoldError):
    """Sentinel: STOP keyword detected by router."""


class WebhookSignatureInvalid(ScaffoldError):
    """X-Hub-Signature-256 header did not verify."""
