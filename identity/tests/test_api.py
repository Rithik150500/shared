import uuid
from unittest.mock import patch

import pytest

from data_access.daos import otp_dao, user_dao
from identity import api as identity_api
from identity.errors import (
    InvalidCredentials,
    InvalidToken,
    OtpInvalid,
    PasswordNotSet,
)
from identity.otp.issuer import hash_otp_code


# --- start_phone_login ---------------------------------------------------

def test_start_phone_login_creates_otp_row(postgresql_session):
    with patch("identity.api.deliver_otp", return_value=("whatsapp", "wamid.X")):
        result = identity_api.start_phone_login(
            postgresql_session, phone="+919876543210", ip_address="203.0.113.5"
        )
    assert "otp_id" in result
    assert result["channel"] == "whatsapp"
    # Verify the row exists
    o = otp_dao.get_by_id(postgresql_session, uuid.UUID(result["otp_id"]))
    assert o is not None
    assert o.delivery_status == "delivered"
    assert o.delivery_provider_id == "wamid.X"


def test_start_phone_login_marks_failed_on_delivery_error(postgresql_session):
    from identity.errors import DeliveryFailed
    with patch("identity.api.deliver_otp", side_effect=DeliveryFailed("whatsapp", "boom")):
        with pytest.raises(DeliveryFailed):
            identity_api.start_phone_login(postgresql_session, phone="+919876543210")
    # The OTP row exists but delivery_status should be 'failed'
    from sqlalchemy import select
    from data_access.models import OtpCode
    rows = postgresql_session.execute(select(OtpCode).where(OtpCode.phone == "+919876543210")).scalars().all()
    assert len(rows) == 1
    assert rows[0].delivery_status == "failed"


def test_start_phone_login_falls_back_to_sms(postgresql_session):
    with patch("identity.api.deliver_otp", return_value=("sms", "REQ123")):
        result = identity_api.start_phone_login(postgresql_session, phone="+919876543210")
    o = otp_dao.get_by_id(postgresql_session, uuid.UUID(result["otp_id"]))
    assert o.channel == "sms"


# --- verify_otp_and_login ------------------------------------------------

def test_verify_otp_creates_new_user_first_time(postgresql_session):
    o = otp_dao.insert(
        postgresql_session, phone="+919876543210", code_hash=hash_otp_code("123456"),
        channel="whatsapp", ttl_minutes=10,
    )
    result = identity_api.verify_otp_and_login(
        postgresql_session, otp_id=o.id, code="123456", brand="munshi"
    )
    assert "access_token" in result
    assert "refresh_token" in result
    assert result["user"]["phone"] == "+919876543210"


def test_verify_otp_nowlez_brand_creates_users_nowlez_extension(postgresql_session):
    from data_access.models import UserNowlez
    o = otp_dao.insert(
        postgresql_session, phone="+919876543210", code_hash=hash_otp_code("123456"),
        channel="whatsapp", ttl_minutes=10,
    )
    identity_api.verify_otp_and_login(
        postgresql_session, otp_id=o.id, code="123456", brand="nowlez", name="Adrika"
    )
    user = user_dao.get_by_phone(postgresql_session, "+919876543210")
    ext = postgresql_session.get(UserNowlez, user.id)
    assert ext is not None
    assert ext.name == "Adrika"


def test_verify_otp_wrong_code_raises(postgresql_session):
    o = otp_dao.insert(
        postgresql_session, phone="+919876543210", code_hash=hash_otp_code("123456"),
        channel="whatsapp", ttl_minutes=10,
    )
    with pytest.raises(OtpInvalid):
        identity_api.verify_otp_and_login(postgresql_session, otp_id=o.id, code="000000", brand="nowlez")


def test_verify_otp_returns_decodable_access_token(postgresql_session):
    from identity import decode_access_token
    o = otp_dao.insert(
        postgresql_session, phone="+919876543210", code_hash=hash_otp_code("123456"),
        channel="whatsapp", ttl_minutes=10,
    )
    result = identity_api.verify_otp_and_login(postgresql_session, otp_id=o.id, code="123456", brand="munshi")
    payload = decode_access_token(result["access_token"])
    assert payload["sub"] == result["user"]["id"]


# --- login_with_password -------------------------------------------------

def test_login_with_password_unknown_phone(postgresql_session):
    with pytest.raises(InvalidCredentials):
        identity_api.login_with_password(postgresql_session, phone="+919999999999", password="anything")


def test_login_with_password_no_password_set(postgresql_session):
    user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    with pytest.raises(PasswordNotSet):
        identity_api.login_with_password(postgresql_session, phone="+919876543210", password="anything")


def test_login_with_password_success(postgresql_session):
    from identity.password.hasher import hash_password
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    user_dao.update_password(postgresql_session, u.id, hash_password("StrongP@ss1"))
    result = identity_api.login_with_password(postgresql_session, phone="+919876543210", password="StrongP@ss1")
    assert "access_token" in result
    assert "refresh_token" in result


# --- refresh + revoke ----------------------------------------------------

def test_refresh_access_token(postgresql_session):
    o = otp_dao.insert(
        postgresql_session, phone="+919876543210", code_hash=hash_otp_code("123456"),
        channel="whatsapp", ttl_minutes=10,
    )
    login = identity_api.verify_otp_and_login(postgresql_session, otp_id=o.id, code="123456", brand="munshi")
    result = identity_api.refresh_access_token(postgresql_session, refresh_token=login["refresh_token"])
    assert "access_token" in result


def test_refresh_invalid_token_raises(postgresql_session):
    with pytest.raises(InvalidToken):
        identity_api.refresh_access_token(postgresql_session, refresh_token="unknown")


def test_revoke_session(postgresql_session):
    o = otp_dao.insert(
        postgresql_session, phone="+919876543210", code_hash=hash_otp_code("123456"),
        channel="whatsapp", ttl_minutes=10,
    )
    login = identity_api.verify_otp_and_login(postgresql_session, otp_id=o.id, code="123456", brand="munshi")
    identity_api.revoke_session(postgresql_session, refresh_token=login["refresh_token"])
    # Subsequent refresh should fail
    with pytest.raises(InvalidToken):
        identity_api.refresh_access_token(postgresql_session, refresh_token=login["refresh_token"])


# --- set_password --------------------------------------------------------

def test_set_password_requires_fresh_otp(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    o = otp_dao.insert(
        postgresql_session, phone="+919876543210", code_hash=hash_otp_code("987654"),
        channel="whatsapp", ttl_minutes=10,
    )
    identity_api.set_password(
        postgresql_session,
        user_id=u.id,
        new_password="NewStrongP@ss1",
        fresh_otp_id=o.id,
        fresh_otp_code="987654",
    )
    u2 = user_dao.get_by_id(postgresql_session, u.id)
    assert u2.password_hash is not None
    # And the new password works
    from identity.password.hasher import verify_password
    assert verify_password("NewStrongP@ss1", u2.password_hash)


def test_set_password_rejects_weak_password(postgresql_session):
    from identity.errors import PasswordTooWeak
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    o = otp_dao.insert(
        postgresql_session, phone="+919876543210", code_hash=hash_otp_code("987654"),
        channel="whatsapp", ttl_minutes=10,
    )
    with pytest.raises(PasswordTooWeak):
        identity_api.set_password(
            postgresql_session,
            user_id=u.id,
            new_password="short",  # < 8 chars
            fresh_otp_id=o.id,
            fresh_otp_code="987654",
        )
