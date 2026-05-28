"""Exception hierarchy for the identity package.

All exceptions inherit from IdentityError so callers can catch broadly or narrowly.
"""
from __future__ import annotations


class IdentityError(Exception):
    """Base class for all identity errors."""


# --- OTP errors ---
class OtpExpired(IdentityError):
    """The OTP code is past its expiry timestamp."""


class OtpAlreadyUsed(IdentityError):
    """The OTP has already been redeemed; request a new one."""


class OtpAttemptsExhausted(IdentityError):
    """All attempts for this OTP code have been used; request a new code."""


class OtpInvalid(IdentityError):
    """The supplied code does not match the stored hash."""


# --- Rate limiting ---
class RateLimited(IdentityError):
    """Caller has exceeded a rate-limit budget."""

    def __init__(self, retry_after_seconds: int = 60):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limit exceeded; retry after {retry_after_seconds}s")


# --- Tokens / sessions ---
class InvalidToken(IdentityError):
    """Token signature is invalid, malformed, or unknown."""


class SessionRevoked(IdentityError):
    """The session was explicitly revoked (e.g., logout)."""


class SessionExpired(IdentityError):
    """The session is past its expiry timestamp."""


# --- Password ---
class InvalidCredentials(IdentityError):
    """Phone or password did not match an active account."""


class PasswordNotSet(IdentityError):
    """The user has no password configured; use OTP login."""


class PasswordTooWeak(IdentityError):
    """The proposed password does not meet strength requirements."""


# --- Account linking ---
class PhoneInUse(IdentityError):
    """The phone number is already linked to a different account."""


# --- Delivery ---
class DeliveryFailed(IdentityError):
    """The chosen delivery channel (WhatsApp/SMS) failed."""

    def __init__(self, channel: str, provider_error: str = ""):
        self.channel = channel
        self.provider_error = provider_error
        super().__init__(f"Delivery failed via {channel}: {provider_error}")
