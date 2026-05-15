import uuid
from datetime import datetime, timedelta, timezone
from data_access.models.auth import AuthSession, OtpCode


def test_auth_session_table_name():
    assert AuthSession.__tablename__ == "auth_sessions"


def test_auth_session_refresh_token_hash_unique():
    col = AuthSession.__table__.c["refresh_token_hash"]
    assert col.unique is True


def test_auth_session_user_id_fk_cascade():
    fks = list(AuthSession.__table__.c["user_id"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"
    assert fks[0].target_fullname == "users.id"


def test_otp_codes_table_name():
    assert OtpCode.__tablename__ == "otp_codes"


def test_otp_default_attempts_metadata():
    col = OtpCode.__table__.c["attempts_remaining"]
    assert col.default.arg == 3


def test_otp_channel_check_constraint_exists():
    constraints = [c for c in OtpCode.__table__.constraints if c.__class__.__name__ == "CheckConstraint"]
    names = {c.name for c in constraints}
    assert "otp_channel_check" in names
    assert "otp_delivery_status_check" in names
