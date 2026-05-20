"""Auto-login JWT for the WhatsApp upsell upgrade flow.

When a Munshi user taps an upsell template's "See plans →" button, the URL
embeds a short-lived signed JWT that, when validated at the Nowlez upgrade
landing endpoint, auto-authenticates the user. Specs:

- 5-minute TTL (short enough to prevent forwarded-link abuse).
- One-time-use via jti tracking (jti is consumed on first validation).
- Separate signing key from auth-JWT (sub-project D's session JWT) —
  cross-key compromise containment.

The JTI consumption tracker is dependency-injected via callables: the
calling Nowlez upgrade landing endpoint supplies a Redis-backed (or
DB-backed via ``consumed_jwt_jti``) tracker without this module having to
know about it. For Phase 1 only the mint/validate primitive ships;
production will swap in the Redis-backed implementation when sub-project
C ships end-to-end.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

import jwt


AUTO_LOGIN_JWT_TTL_MINUTES = 5
AUTO_LOGIN_JWT_PURPOSE = "upgrade_auto_login"


def mint_auto_login_jwt(user_id: uuid.UUID, secret: str) -> str:
    """Mint a 5-min-TTL JWT for the given user_id.

    Args:
        user_id: User who tapped the WhatsApp upsell CTA.
        secret: Auto-login JWT signing secret. MUST be distinct from the
            session-JWT secret used by sub-project D — see module docstring.

    Returns:
        Encoded JWT string (HS256). Caller embeds in the upgrade URL.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "purpose": AUTO_LOGIN_JWT_PURPOSE,
        "exp": int(
            (now + timedelta(minutes=AUTO_LOGIN_JWT_TTL_MINUTES)).timestamp()
        ),
        "iat": int(now.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def validate_auto_login_jwt(
    token: str,
    secret: str,
    *,
    is_jti_consumed: Callable[[str], bool],
    mark_jti_consumed: Callable[[str], None],
) -> uuid.UUID | None:
    """Validate the JWT.

    Returns user_id on success; ``None`` on expired / invalid signature /
    wrong-purpose / replay (jti consumed).

    ``is_jti_consumed`` and ``mark_jti_consumed`` are dependency-injected
    so the caller (the Nowlez upgrade landing endpoint) supplies a
    Redis-backed or DB-backed tracker without this module having to know
    about it.

    Note: the order of the jti check is "check first, then mark". A second
    concurrent validation racing on the same token will both pass the
    "is consumed" check before either marks — production trackers SHOULD
    use an atomic check-and-set primitive (Redis SETNX, Postgres INSERT
    with ON CONFLICT DO NOTHING) and report consumed=True on the loser
    side. The in-memory test tracker is single-threaded so a sequential
    check+mark is sufficient there.
    """
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None

    if payload.get("purpose") != AUTO_LOGIN_JWT_PURPOSE:
        return None

    jti = payload.get("jti")
    if not jti:
        return None
    if is_jti_consumed(jti):
        return None
    mark_jti_consumed(jti)

    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        return None


__all__ = [
    "AUTO_LOGIN_JWT_TTL_MINUTES",
    "AUTO_LOGIN_JWT_PURPOSE",
    "mint_auto_login_jwt",
    "validate_auto_login_jwt",
]
