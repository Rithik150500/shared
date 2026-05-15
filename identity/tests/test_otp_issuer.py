import re

from identity.otp.issuer import generate_otp_code, hash_otp_code


def test_generate_otp_code_format():
    code = generate_otp_code()
    assert re.fullmatch(r"\d{6}", code)


def test_generate_otp_codes_are_random():
    # Cryptographic RNG should produce near-collision-free 6-digit codes over 50 samples
    seen = {generate_otp_code() for _ in range(50)}
    assert len(seen) >= 45  # allow a few accidental collisions; should be ~50 in practice


def test_hash_otp_code_format():
    h = hash_otp_code("123456")
    assert h.startswith("$argon2id$")


def test_hash_otp_code_is_salted():
    # Same code, two different hashes (argon2id includes per-call salt)
    h1 = hash_otp_code("123456")
    h2 = hash_otp_code("123456")
    assert h1 != h2


def test_hash_otp_code_verifies_correctly():
    # Roundtrip via verification (used by Task 22 verifier)
    from argon2 import PasswordHasher
    h = hash_otp_code("987654")
    ph = PasswordHasher()
    ph.verify(h, "987654")  # should not raise
