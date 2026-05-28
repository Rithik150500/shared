import re

import pytest

from identity.password.hasher import hash_password, verify_password, BOGUS_HASH, needs_rehash
from identity.password.validator import validate_password_strength
from identity.errors import PasswordTooWeak


def test_hash_verify_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong-password", h)


def test_hash_is_argon2id():
    h = hash_password("any-password-9!")
    assert re.match(r"^\$argon2id\$", h)


def test_bogus_hash_is_valid_argon2id():
    assert re.match(r"^\$argon2id\$", BOGUS_HASH)


def test_verify_bogus_hash_against_anything_returns_false():
    # Used for timing-safe login: even when user not found, we run verify
    # against the bogus hash so the timing is similar to a real failed verify.
    assert not verify_password("anything", BOGUS_HASH)


def test_needs_rehash_returns_bool():
    h = hash_password("strong!Password9")
    # With unchanged params, should not need rehash
    assert needs_rehash(h) is False


def test_validate_rejects_too_short():
    with pytest.raises(PasswordTooWeak, match="at least 8"):
        validate_password_strength("short")


def test_validate_rejects_common_password():
    with pytest.raises(PasswordTooWeak, match="too common"):
        validate_password_strength("password")


def test_validate_rejects_common_password_case_insensitive():
    with pytest.raises(PasswordTooWeak, match="too common"):
        validate_password_strength("Password")  # capitalized variant of "password"


def test_validate_accepts_strong_password():
    # Should not raise
    validate_password_strength("X9!correctHorse")


def test_verify_password_returns_false_on_bcrypt_hash():
    """Migrated email users carry a *bcrypt* password_hash in Postgres
    (copied verbatim from SQLite by g_users_backfill). argon2 verify must
    treat a non-argon2 hash as a clean mismatch (False) rather than raising —
    otherwise /api/auth/login-password would 500 for a migrated user. Locks
    the sub-project D cutover invariant."""
    import bcrypt

    from identity.password.hasher import verify_password

    bcrypt_hash = bcrypt.hashpw(b"hunter2", bcrypt.gensalt()).decode()
    assert verify_password("hunter2", bcrypt_hash) is False
    assert verify_password("wrong", bcrypt_hash) is False
