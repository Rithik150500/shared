import pytest

from data_access.daos import otp_dao
from identity.errors import OtpAlreadyUsed, OtpAttemptsExhausted, OtpExpired, OtpInvalid
from identity.otp.issuer import hash_otp_code
from identity.otp.verifier import verify_otp


def test_verify_success_marks_used(postgresql_session):
    code_hash = hash_otp_code("123456")
    o = otp_dao.insert(postgresql_session, phone="+919876543210", code_hash=code_hash, channel="whatsapp", ttl_minutes=10)
    verify_otp(postgresql_session, otp_id=o.id, code="123456")
    o2 = otp_dao.get_by_id(postgresql_session, o.id)
    assert o2.used_at is not None


def test_verify_wrong_code_decrements_attempts(postgresql_session):
    code_hash = hash_otp_code("123456")
    o = otp_dao.insert(postgresql_session, phone="+919876543210", code_hash=code_hash, channel="whatsapp", ttl_minutes=10)
    with pytest.raises(OtpInvalid):
        verify_otp(postgresql_session, otp_id=o.id, code="000000")
    o2 = otp_dao.get_by_id(postgresql_session, o.id)
    assert o2.attempts_remaining == 2
    assert o2.used_at is None


def test_verify_expired(postgresql_session):
    code_hash = hash_otp_code("123456")
    o = otp_dao.insert(postgresql_session, phone="+919876543210", code_hash=code_hash, channel="whatsapp", ttl_minutes=-1)
    with pytest.raises(OtpExpired):
        verify_otp(postgresql_session, otp_id=o.id, code="123456")


def test_verify_already_used(postgresql_session):
    code_hash = hash_otp_code("123456")
    o = otp_dao.insert(postgresql_session, phone="+919876543210", code_hash=code_hash, channel="whatsapp", ttl_minutes=10)
    otp_dao.mark_used(postgresql_session, o.id)
    with pytest.raises(OtpAlreadyUsed):
        verify_otp(postgresql_session, otp_id=o.id, code="123456")


def test_verify_attempts_exhausted(postgresql_session):
    code_hash = hash_otp_code("123456")
    o = otp_dao.insert(postgresql_session, phone="+919876543210", code_hash=code_hash, channel="whatsapp", ttl_minutes=10)
    for _ in range(3):
        otp_dao.decrement_attempts(postgresql_session, o.id)
    with pytest.raises(OtpAttemptsExhausted):
        verify_otp(postgresql_session, otp_id=o.id, code="123456")


def test_verify_unknown_otp_id_raises(postgresql_session):
    import uuid
    with pytest.raises(OtpInvalid):
        verify_otp(postgresql_session, otp_id=uuid.uuid4(), code="123456")
