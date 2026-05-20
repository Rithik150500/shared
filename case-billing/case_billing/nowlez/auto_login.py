"""Auto-login JWT for the WhatsApp upsell upgrade flow.

When a Munshi user taps an upsell template's "See plans →" button, the URL
embeds a short-lived signed JWT that, when validated at the Nowlez upgrade
landing endpoint, auto-authenticates the user. Specs:

- 5-minute TTL (short enough to prevent forwarded-link abuse).
- One-time-use via jti tracking (jti is consumed on first validation).
- Separate signing key from auth-JWT (sub-project D's session JWT) —
  cross-key compromise containment.

The JTI consumption tracker is dependency-injected via a single atomic
``consume_jti`` callable: the calling Nowlez upgrade landing endpoint
supplies a Redis-backed (SETNX) or DB-backed (INSERT … ON CONFLICT DO
NOTHING) implementation without this module having to know about it.
Production swaps in the Redis-backed implementation at the casepilot
``upgrade_landing`` endpoint (``casepilot/backend/routers/auth.py``);
see ``_consume_auto_login_jti`` there.
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
    consume_jti: Callable[[str], bool],
) -> uuid.UUID | None:
    """Validate the JWT.

    Returns user_id on success; ``None`` on expired / invalid signature /
    wrong-purpose / replay (jti already consumed).

    ``consume_jti`` is a single atomic dependency-injected callable that
    MUST claim-or-skip the jti in one step and return:

    * ``True`` if the jti was fresh and is now consumed (caller is the
      winner — proceed with auth).
    * ``False`` if the jti was already consumed (replay — caller is the
      loser, including the concurrent-pod race case).

    Audit fix C-2: the previous two-callable (``is_jti_consumed`` +
    ``mark_jti_consumed``) shape was TOCTOU-vulnerable across processes
    — two pods could both observe "not consumed" and both call "mark"
    before either claim was visible. The single-callable contract makes
    the atomic check-and-set primitive (Redis SETNX, Postgres INSERT
    with ON CONFLICT DO NOTHING) the implementer's responsibility,
    eliminating the race at the API level.
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
    if not consume_jti(jti):
        return None  # replay (already consumed; possibly by another pod)

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
