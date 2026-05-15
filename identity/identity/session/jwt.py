"""Access-token JWT encode/decode (HS256, 30-minute TTL by default)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt

from ..config import settings
from ..errors import InvalidToken


def encode_access_token(user_id: uuid.UUID, *, ttl_seconds: int | None = None) -> str:
    """Mint a short-lived access JWT bound to user_id.

    Default TTL is settings.JWT_ACCESS_TTL_MINUTES (30 min). Pass ttl_seconds
    for tests or alternative lifetimes.
    """
    now = datetime.now(timezone.utc)
    ttl = (
        timedelta(seconds=ttl_seconds)
        if ttl_seconds is not None
        else timedelta(minutes=settings.JWT_ACCESS_TTL_MINUTES)
    )
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return pyjwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Verify signature + expiry; return claims dict or raise InvalidToken."""
    try:
        return pyjwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except pyjwt.ExpiredSignatureError:
        raise InvalidToken("access token expired")
    except pyjwt.InvalidTokenError as e:
        raise InvalidToken(f"invalid access token: {e}")
