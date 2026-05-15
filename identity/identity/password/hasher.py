"""Password hashing via argon2id.

Uses the parameters configured in identity.config.settings (OWASP 2026 defaults
unless overridden by env var).
"""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2 import exceptions as a2err

from ..config import settings

_hasher = PasswordHasher(
    time_cost=settings.ARGON2_TIME_COST,
    memory_cost=settings.ARGON2_MEMORY_COST_KB,
    parallelism=settings.ARGON2_PARALLELISM,
)

# Pre-computed argon2id hash of a throwaway value.
# Used for timing-parity: when login is attempted with an unknown phone, we still
# run argon2id verify against this bogus hash so the response time matches a
# real "wrong password" outcome.
BOGUS_HASH = _hasher.hash("__bogus__")


def hash_password(plain: str) -> str:
    """Return an argon2id hash string for the given plaintext password."""
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """True iff `plain` verifies against `hashed`. Returns False on any mismatch."""
    try:
        _hasher.verify(hashed, plain)
        return True
    except (a2err.VerifyMismatchError, a2err.InvalidHash):
        return False


def needs_rehash(hashed: str) -> bool:
    """True iff the stored hash was produced with weaker params than current settings.

    Call after a successful verify to opportunistically upgrade old hashes.
    """
    return _hasher.check_needs_rehash(hashed)
