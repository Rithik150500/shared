"""Server-side verification of a Google ID token (OIDC `credential`).

The SPA's Google Identity Services button hands back a signed JWT (`credential`).
This module verifies it against Google's public keys and the configured audience
so the backend never trusts a client-supplied email. Verification covers the
signature, expiry, issuer (accounts.google.com), and audience.

`google-auth` is imported LAZILY inside the function so the rest of the identity
package (and its unit tests, which patch `identity.api.verify_google_id_token`)
import cleanly even where google-auth is not installed.
"""
from __future__ import annotations

from ..config import settings
from ..errors import GoogleTokenInvalid


def verify_google_id_token(id_token: str, *, audience: str) -> dict:
    """Verify a Google ID token and return its claims.

    Args:
        id_token: the raw Google `credential` JWT from the browser.
        audience: the OAuth 2.0 Web client id the token must be addressed to.

    Returns:
        The decoded, verified claims (sub, email, email_verified, name, ...).

    Raises:
        GoogleTokenInvalid: on any signature/audience/issuer/expiry failure or a
            malformed token. The caller (identity.api) additionally enforces the
            `email_verified` and `email`/`sub` presence checks.
    """
    if not id_token:
        raise GoogleTokenInvalid("missing Google credential")
    if not audience:
        raise GoogleTokenInvalid("Google login is not configured")

    # Lazy import: keeps `import identity` working without google-auth installed.
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token
    except ImportError as exc:  # pragma: no cover - install-time guard
        raise GoogleTokenInvalid(
            "google-auth is not installed; cannot verify Google tokens"
        ) from exc

    # google-auth's verify_oauth2_token does not pass a timeout to the cert
    # (JWKS) fetch, so it falls back to the transport's 120s default — long
    # enough to tie up a sync worker if Google's cert endpoint hangs. Bind it to
    # GOOGLE_TOKEN_VERIFY_TIMEOUT_SECONDS via a thin Request subclass.
    fetch_timeout = settings.GOOGLE_TOKEN_VERIFY_TIMEOUT_SECONDS

    class _TimeoutRequest(google_requests.Request):
        def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
            return super().__call__(
                url, method=method, body=body, headers=headers,
                timeout=timeout if timeout is not None else fetch_timeout, **kwargs,
            )

    try:
        # verify_oauth2_token checks signature, exp, aud == audience, and that
        # iss is accounts.google.com / https://accounts.google.com.
        claims = google_id_token.verify_oauth2_token(
            id_token, _TimeoutRequest(), audience
        )
    except ValueError as exc:
        # google-auth raises ValueError for every verification failure.
        raise GoogleTokenInvalid(f"Google token verification failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - transport/cert-fetch failures
        raise GoogleTokenInvalid(f"Google token verification error: {exc}") from exc

    if not isinstance(claims, dict):  # defensive
        raise GoogleTokenInvalid("Google token did not decode to claims")
    return claims
