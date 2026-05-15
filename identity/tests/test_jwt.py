import time
import uuid

import pytest

from identity.session.jwt import encode_access_token, decode_access_token
from identity.errors import InvalidToken


def test_encode_decode_roundtrip():
    uid = uuid.uuid4()
    token = encode_access_token(uid)
    payload = decode_access_token(token)
    assert payload["sub"] == str(uid)
    assert "exp" in payload
    assert "iat" in payload


def test_decode_rejects_tampered_signature():
    uid = uuid.uuid4()
    token = encode_access_token(uid)
    # Corrupt the last 4 chars of the signature segment
    parts = token.split(".")
    parts[2] = "AAAA" + parts[2][4:]
    with pytest.raises(InvalidToken):
        decode_access_token(".".join(parts))


def test_decode_rejects_garbage():
    with pytest.raises(InvalidToken):
        decode_access_token("not-a-jwt-at-all")


def test_decode_rejects_expired_token():
    uid = uuid.uuid4()
    # ttl_seconds=1 — sleep past it
    token = encode_access_token(uid, ttl_seconds=1)
    time.sleep(2)
    with pytest.raises(InvalidToken):
        decode_access_token(token)


def test_decode_rejects_wrong_secret(monkeypatch):
    uid = uuid.uuid4()
    token = encode_access_token(uid)
    # Re-import config with a different secret — the existing token should no longer decode
    monkeypatch.setenv("JWT_SECRET_KEY", "different-secret")
    from importlib import reload
    import identity.config as cfg
    import identity.session.jwt as jwt_mod
    reload(cfg)
    reload(jwt_mod)
    with pytest.raises(InvalidToken):
        jwt_mod.decode_access_token(token)
