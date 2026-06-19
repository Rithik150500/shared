import uuid

from data_access.models.auth import LoginRequest, EmailOtpCode


def test_login_requests_table_name():
    assert LoginRequest.__tablename__ == "login_requests"


def test_email_otp_codes_table_name():
    assert EmailOtpCode.__tablename__ == "email_otp_codes"


def test_login_request_id_default_is_uuid4():
    # Python-side default so SQLite Base.metadata.create_all works (no literal gen_random_uuid()).
    # SQLAlchemy may wrap the callable; unwrap if needed to check identity.
    fn = LoginRequest.__table__.c["id"].default.arg
    assert fn is uuid.uuid4 or getattr(fn, "__wrapped__", None) is uuid.uuid4


def test_email_otp_id_default_is_uuid4():
    fn = EmailOtpCode.__table__.c["id"].default.arg
    assert fn is uuid.uuid4 or getattr(fn, "__wrapped__", None) is uuid.uuid4


def test_login_request_token_hash_unique():
    assert LoginRequest.__table__.c["token_hash"].unique is True


def test_login_request_user_id_fk_cascade():
    fks = list(LoginRequest.__table__.c["user_id"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"
    assert fks[0].target_fullname == "users.id"


def test_login_request_user_id_nullable():
    # NULL until confirm for web2bot.
    assert LoginRequest.__table__.c["user_id"].nullable is True


def test_login_request_check_constraints_present():
    names = {
        c.name
        for c in LoginRequest.__table__.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert "login_requests_direction_check" in names
    assert "login_requests_status_check" in names
    assert "login_requests_brand_check" in names


def test_email_otp_check_constraint_present():
    names = {
        c.name
        for c in EmailOtpCode.__table__.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert "email_otp_delivery_status_check" in names


def test_email_otp_default_attempts_is_three():
    assert EmailOtpCode.__table__.c["attempts_remaining"].default.arg == 3


def test_status_server_default_is_pending():
    assert LoginRequest.__table__.c["status"].server_default.arg == "pending"


def test_email_otp_delivery_status_server_default_is_pending():
    assert EmailOtpCode.__table__.c["delivery_status"].server_default.arg == "pending"
