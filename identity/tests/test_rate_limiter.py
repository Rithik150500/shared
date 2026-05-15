import pytest

from data_access.daos import otp_dao
from identity.errors import RateLimited
from identity.otp.rate_limiter import check_otp_rate_limit


def test_under_limit_passes(postgresql_session):
    # No prior OTPs — should pass
    check_otp_rate_limit(postgresql_session, phone="+919876543210", ip_address="203.0.113.5")


def test_per_phone_limit_blocks(postgresql_session):
    for _ in range(5):
        otp_dao.insert(postgresql_session, phone="+919876543210", code_hash="x", channel="whatsapp", ttl_minutes=10)
    with pytest.raises(RateLimited):
        check_otp_rate_limit(postgresql_session, phone="+919876543210", ip_address="203.0.113.5")


def test_per_phone_limit_at_boundary(postgresql_session):
    # 4 prior is OK (5th allowed)
    for _ in range(4):
        otp_dao.insert(postgresql_session, phone="+919876543210", code_hash="x", channel="whatsapp", ttl_minutes=10)
    check_otp_rate_limit(postgresql_session, phone="+919876543210", ip_address="203.0.113.5")


def test_per_ip_limit_blocks(postgresql_session):
    # 20 OTPs from same IP across different phones
    for i in range(20):
        otp_dao.insert(postgresql_session, phone=f"+9198765432{i:02d}", code_hash="x", channel="whatsapp", ttl_minutes=10, ip_address="203.0.113.5")
    with pytest.raises(RateLimited):
        check_otp_rate_limit(postgresql_session, phone="+919876543299", ip_address="203.0.113.5")


def test_no_ip_address_only_checks_phone(postgresql_session):
    # When ip_address=None, skip per-IP check; per-phone still applies
    for _ in range(2):
        otp_dao.insert(postgresql_session, phone="+919876543210", code_hash="x", channel="whatsapp", ttl_minutes=10)
    # ip_address=None — should not raise
    check_otp_rate_limit(postgresql_session, phone="+919876543210", ip_address=None)


def test_rate_limit_retry_after_is_set(postgresql_session):
    for _ in range(5):
        otp_dao.insert(postgresql_session, phone="+919876543210", code_hash="x", channel="whatsapp", ttl_minutes=10)
    try:
        check_otp_rate_limit(postgresql_session, phone="+919876543210", ip_address="203.0.113.5")
        assert False, "Should have raised"
    except RateLimited as e:
        assert e.retry_after_seconds == 3600
