"""Tests for the canonical phone normalizer.

The root cause of the Munshi "portfolio is empty" identity split was that the
WhatsApp webhook stores phones as E.164 (+91...) while the web/OTP provisioning
path stored bare 10-digit strings. UNIQUE(phone) treated them as distinct keys
→ duplicate user rows. normalize_phone() is the single canonical form that every
write/lookup path funnels through so the two converge.
"""
from data_access.phone import normalize_phone


def test_none_and_empty_return_none():
    assert normalize_phone(None) is None
    assert normalize_phone("") is None
    assert normalize_phone("   ") is None


def test_bare_10_digit_indian_gets_plus91():
    assert normalize_phone("9953652710") == "+919953652710"


def test_already_e164_indian_is_unchanged():
    assert normalize_phone("+919953652710") == "+919953652710"


def test_91_prefixed_without_plus_gets_plus():
    assert normalize_phone("919953652710") == "+919953652710"


def test_formatting_is_stripped():
    assert normalize_phone("+91 99536-52710") == "+919953652710"
    assert normalize_phone("(995) 365-2710") == "+919953652710"


def test_national_trunk_leading_zero_is_dropped():
    assert normalize_phone("099536 52710") == "+919953652710"


def test_foreign_e164_country_code_is_preserved():
    assert normalize_phone("+447723442078") == "+447723442078"
    assert normalize_phone("+15559542580") == "+15559542580"


def test_normalization_is_idempotent():
    for raw in ["9953652710", "+919953652710", "919953652710", "+447723442078"]:
        once = normalize_phone(raw)
        assert normalize_phone(once) == once


def test_default_country_is_configurable():
    assert normalize_phone("5559542580", default_cc="1") == "+15559542580"


def test_international_access_00_prefix_is_treated_as_plus():
    # Indian dialers/paste often use the 00 international-access code instead of +.
    assert normalize_phone("00919953652710") == "+919953652710"
    assert normalize_phone("00 91 99536 52710") == "+919953652710"


def test_foreign_number_without_plus_preserves_country_code():
    # 11+ digits, no plus, no trunk zero ⇒ country code already present; never +91.
    assert normalize_phone("447723442078") == "+447723442078"
