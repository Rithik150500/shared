"""Phase 1: auto_login JWT mint/validate.

Covers the WhatsApp upsell auto-login JWT primitive:
- Round-trip mint + validate returns the original user_id.
- Expired token returns None.
- Wrong-purpose token returns None (cross-key compromise containment).
- Replay (jti consumed) returns None.
- Wrong signing secret returns None.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from case_billing.nowlez.auto_login import (
    AUTO_LOGIN_JWT_PURPOSE,
    AUTO_LOGIN_JWT_TTL_MINUTES,
    mint_auto_login_jwt,
    validate_auto_login_jwt,
)


SECRET = "test-secret"


@pytest.fixture
def tracker():
    """In-memory jti tracker matching the prod tracker interface."""
    consumed: set[str] = set()
    return {
        "is_consumed": lambda jti: jti in consumed,
        "mark_consumed": lambda jti: consumed.add(jti),
        "consumed": consumed,
    }


def test_mint_and_validate_round_trip(tracker):
    user_id = uuid.uuid4()
    token = mint_auto_login_jwt(user_id, SECRET)
    result = validate_auto_login_jwt(
        token, SECRET,
        is_jti_consumed=tracker["is_consumed"],
        mark_jti_consumed=tracker["mark_consumed"],
    )
    assert result == user_id


def test_validate_returns_none_for_expired_jwt(tracker):
    # Forge an already-expired token.
    user_id = uuid.uuid4()
    payload = {
        "sub": str(user_id),
        "purpose": AUTO_LOGIN_JWT_PURPOSE,
        "exp": int(
            (datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp()
        ),
        "iat": int(datetime.now(timezone.utc).timestamp()) - 10,
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, SECRET, algorithm="HS256")
    result = validate_auto_login_jwt(
        token, SECRET,
        is_jti_consumed=tracker["is_consumed"],
        mark_jti_consumed=tracker["mark_consumed"],
    )
    assert result is None


def test_validate_returns_none_for_wrong_purpose(tracker):
    user_id = uuid.uuid4()
    payload = {
        "sub": str(user_id),
        "purpose": "different_purpose",
        "exp": int(
            (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
        ),
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, SECRET, algorithm="HS256")
    result = validate_auto_login_jwt(
        token, SECRET,
        is_jti_consumed=tracker["is_consumed"],
        mark_jti_consumed=tracker["mark_consumed"],
    )
    assert result is None


def test_validate_returns_none_for_replay(tracker):
    user_id = uuid.uuid4()
    token = mint_auto_login_jwt(user_id, SECRET)
    # First validation succeeds.
    first = validate_auto_login_jwt(
        token, SECRET,
        is_jti_consumed=tracker["is_consumed"],
        mark_jti_consumed=tracker["mark_consumed"],
    )
    assert first == user_id
    # Second validation (replay) must fail because jti was marked consumed.
    second = validate_auto_login_jwt(
        token, SECRET,
        is_jti_consumed=tracker["is_consumed"],
        mark_jti_consumed=tracker["mark_consumed"],
    )
    assert second is None


def test_validate_returns_none_for_invalid_signature(tracker):
    user_id = uuid.uuid4()
    token = mint_auto_login_jwt(user_id, SECRET)
    result = validate_auto_login_jwt(
        token, "wrong-secret",
        is_jti_consumed=tracker["is_consumed"],
        mark_jti_consumed=tracker["mark_consumed"],
    )
    assert result is None


def test_ttl_constant_matches_spec():
    """Sanity check — spec mandates 5-minute TTL."""
    assert AUTO_LOGIN_JWT_TTL_MINUTES == 5
