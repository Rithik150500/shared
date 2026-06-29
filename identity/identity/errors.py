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


class RefreshTokenReuse(InvalidToken):
    """A refresh token that was already ROTATED (replaced) was presented again
    beyond the concurrency grace window — a replay / theft signal. The whole
    session family is revoked and the caller audits 'auth.refresh_replay'.

    Subclasses InvalidToken so existing ``except InvalidToken`` handlers (the
    web layer maps it to 401) keep working; catch this FIRST when an auditable
    distinction is needed. Carries user_id + family_id for the audit event."""

    def __init__(self, *, user_id=None, family_id=None):
        self.user_id = user_id
        self.family_id = family_id
        super().__init__("refresh token reuse detected; session family revoked")


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


# --- Delivery ---
class DeliveryFailed(IdentityError):
    """The chosen delivery channel (WhatsApp/SMS) failed."""

    def __init__(self, channel: str, provider_error: str = ""):
        self.channel = channel
        self.provider_error = provider_error
        super().__init__(f"Delivery failed via {channel}: {provider_error}")


class EmailDeliveryFailed(DeliveryFailed):
    """Email OTP delivery (Resend) failed. Soft-fail: caller still returns 200
    (anti-enumeration) but marks the row failed + bumps the fail metric."""

    def __init__(self, provider_error: str = ""):
        super().__init__("email", provider_error)


# --- Account linking (D4) ---
class AccountLinkStepUpRequired(IdentityError):
    """An email-OTP / Google verify landed on an existing phone/password account
    whose email is not yet verified, and no second proven identifier is present.
    The SPA must prompt 'verify by phone to link this email' (HTTP 409)."""


# --- Federated identity (Google Sign-In) ---
class GoogleTokenInvalid(IdentityError):
    """The supplied Google ID token failed verification: bad signature, wrong
    audience/issuer, expired, missing subject/email, or an unverified email.
    The web layer maps this to HTTP 401."""
