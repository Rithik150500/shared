"""OTP phone normalization (PG-only: OtpCode.created_at uses func.now()).

Belt-and-suspenders for the phone-format split: the stored OTP phone is
canonical (so it flows into get_or_create_by_phone as +91...), and the
per-phone rate limit can't be bypassed by alternating bare / +91 forms.
"""
import pytest

from data_access.daos import otp_dao


def test_insert_rejects_phone_that_normalizes_to_none(db_session):
    # Guards M1: OtpCode.phone is NOT NULL — a junk/empty phone must raise a
    # clear ValueError before the DB op, not an opaque IntegrityError (500).
    for junk in ["", "   ", "+", "abc"]:
        with pytest.raises(ValueError):
            otp_dao.insert(db_session, phone=junk, code_hash="h", channel="whatsapp")


def test_insert_stores_canonical_phone(postgresql_session):
    o = otp_dao.insert(
        postgresql_session, phone="9953652710", code_hash="h", channel="whatsapp",
    )
    assert o.phone == "+919953652710"


def test_get_active_matches_across_formats(postgresql_session):
    created = otp_dao.insert(
        postgresql_session, phone="+919953652710", code_hash="h", channel="whatsapp",
    )
    found = otp_dao.get_active(postgresql_session, "9953652710")
    assert found is not None
    assert found.id == created.id


def test_count_within_not_bypassable_by_phone_format(postgresql_session):
    otp_dao.insert(postgresql_session, phone="9953652710", code_hash="h", channel="whatsapp")
    otp_dao.insert(postgresql_session, phone="+919953652710", code_hash="h", channel="sms")
    # Both requests are the same person; the rate limiter must count both.
    assert otp_dao.count_within(postgresql_session, "919953652710", 60) == 2
