"""Phase 1: auto_login JWT mint/validate.

Covers the WhatsApp upsell auto-login JWT primitive:
- Round-trip mint + validate returns the original user_id.
- Expired token returns None.
- Wrong-purpose token returns None (cross-key compromise containment).
- Replay (jti consumed) returns None.
- Wrong signing secret returns None.

Audit fix C-2: the previous two-callable (is_jti_consumed +
mark_jti_consumed) shape was replaced with a single atomic
``consume_jti`` callable to eliminate the cross-pod TOCTOU race.
The in-memory tracker below mirrors a Redis SETNX: it returns True
on the first call for a given jti, False thereafter.
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


def _make_consume_fn():
    """In-memory atomic jti tracker matching the prod (Redis SETNX) contract.

    Returns a ``consume`` callable that returns True the first time a
    given jti is seen (claim succeeded) and False thereafter (replay).
    """
    consumed: set[str] = set()

    def consume(jti: str) -> bool:
        if jti in consumed:
            return False
        consumed.add(jti)
        return True

    return consume


@pytest.fixture
def consume_jti():
    return _make_consume_fn()


def test_mint_and_validate_round_trip(consume_jti):
    user_id = uuid.uuid4()
    token = mint_auto_login_jwt(user_id, SECRET)
    result = validate_auto_login_jwt(
        token, SECRET,
        consume_jti=consume_jti,
    )
    assert result == user_id


def test_validate_returns_none_for_expired_jwt(consume_jti):
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
        consume_jti=consume_jti,
    )
    assert result is None


def test_validate_returns_none_for_wrong_purpose(consume_jti):
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
        consume_jti=consume_jti,
    )
    assert result is None


def test_validate_returns_none_for_replay(consume_jti):
    user_id = uuid.uuid4()
    token = mint_auto_login_jwt(user_id, SECRET)
    # First validation succeeds (consume_jti returns True).
    first = validate_auto_login_jwt(
        token, SECRET,
        consume_jti=consume_jti,
    )
    assert first == user_id
    # Second validation (replay) must fail because consume_jti now returns
    # False for that jti.
    second = validate_auto_login_jwt(
        token, SECRET,
        consume_jti=consume_jti,
    )
    assert second is None


def test_validate_returns_none_for_invalid_signature(consume_jti):
    user_id = uuid.uuid4()
    token = mint_auto_login_jwt(user_id, SECRET)
    result = validate_auto_login_jwt(
        token, "wrong-secret",
        consume_jti=consume_jti,
    )
    assert result is None


def test_ttl_constant_matches_spec():
    """Sanity check — spec mandates 5-minute TTL."""
    assert AUTO_LOGIN_JWT_TTL_MINUTES == 5


def test_consume_jti_loser_side_returns_none():
    """Audit fix C-2: when consume_jti reports the caller is the loser of
    a concurrent claim race (returns False on the very first call to
    validate for that token), validate_auto_login_jwt must return None
    so the loser pod can't auth.
    """
    def always_loses(_jti: str) -> bool:
        return False

    user_id = uuid.uuid4()
    token = mint_auto_login_jwt(user_id, SECRET)
    result = validate_auto_login_jwt(
        token, SECRET,
        consume_jti=always_loses,
    )
    assert result is None
