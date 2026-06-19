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
