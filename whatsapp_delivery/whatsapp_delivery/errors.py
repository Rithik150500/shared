"""Scaffold error taxonomy.

Distinct from ecourts_client errors: these signal failures in the bot infrastructure
itself (Meta API, rate limits, internal misconfiguration) rather than in the eCourts
data layer.
"""
from __future__ import annotations


class ScaffoldError(Exception):
    """Base class for all bot-scaffold errors."""


class MetaTransientError(ScaffoldError):
    """Meta returned 5xx or a network glitch. Caller should retry."""


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
