from data_access.daos import otp_dao, user_dao
from identity import api as identity_api
from identity.otp.issuer import hash_otp_code

EXPECTED_TOP_KEYS = {"access_token", "refresh_token", "user"}
EXPECTED_USER_KEYS = {"id", "phone", "locale"}


def test_mint_login_response_shape(db_session):
    u, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    db_session.flush()
    out = identity_api._mint_login_response(db_session, user=u, brand="nowlez", name="A")
    assert set(out) == EXPECTED_TOP_KEYS
    assert set(out["user"]) == EXPECTED_USER_KEYS
    assert out["user"]["id"] == str(u.id)


def test_verify_otp_and_login_uses_mint_shape(db_session):
    o = otp_dao.insert(db_session, phone="+919811111111", code_hash=hash_otp_code("123456"), channel="whatsapp")
    db_session.flush()
    out = identity_api.verify_otp_and_login(db_session, otp_id=o.id, code="123456", brand="munshi")
    assert set(out) == EXPECTED_TOP_KEYS
    assert set(out["user"]) == EXPECTED_USER_KEYS


def test_three_login_paths_structurally_identical(db_session):
    from data_access.daos import email_otp_dao, login_request_dao, session_dao
    import uuid as _uuid

    # path 1: phone OTP
    o = otp_dao.insert(db_session, phone="+919800000001", code_hash=hash_otp_code("111111"), channel="whatsapp")
    db_session.flush()
    a = identity_api.verify_otp_and_login(db_session, otp_id=o.id, code="111111", brand="munshi")

    # path 2: wa-login consume (web2bot)
    u2, _ = user_dao.get_or_create_by_phone(db_session, phone="+919800000002")
    db_session.flush()
    start = identity_api.start_wa_login(db_session, brand="nowlez")
    db_session.flush()
    identity_api.confirm_wa_login(db_session, nonce=start["nonce"], user=u2, brand="munshi")
    b = identity_api.consume_wa_login(
        db_session, login_id=_uuid.UUID(start["login_id"]), poll_bind=start["poll_secret"]
    )

    # path 3: email OTP verify (verify_email_otp_and_login — new signup branch)
    eo = email_otp_dao.insert(
        db_session, email="drift@example.com", code_hash=hash_otp_code("999999")
    )
    db_session.flush()
    c = identity_api.verify_email_otp_and_login(
        db_session, otp_id=eo.id, code="999999", brand="nowlez", name="Drift"
    )

    assert set(a) == set(b) == set(c)
    assert set(a["user"]) == set(b["user"]) == set(c["user"]) == {"id", "phone", "locale"}
