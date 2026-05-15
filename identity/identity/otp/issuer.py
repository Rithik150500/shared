"""OTP issuance — generate a cryptographically-random N-digit code and hash it.

The plaintext code is what the user types; the hash is what we persist (via
otp_dao.insert in data_access). Never log or persist the plaintext code.
"""
from __future__ import annotations

import secrets

from argon2 import PasswordHasher

from ..config import settings

_otp_hasher = PasswordHasher(
    time_cost=settings.ARGON2_TIME_COST,
    memory_cost=settings.ARGON2_MEMORY_COST_KB,
    parallelism=settings.ARGON2_PARALLELISM,
)


def generate_otp_code() -> str:
    """Return a fresh N-digit OTP (N = settings.OTP_LENGTH, default 6)."""
    n = settings.OTP_LENGTH
    upper = 10 ** n
    code = secrets.randbelow(upper)
    return f"{code:0{n}d}"


def hash_otp_code(code: str) -> str:
    """Argon2id-hash the OTP. Same input produces different output each call (salted)."""
    return _otp_hasher.hash(code)
