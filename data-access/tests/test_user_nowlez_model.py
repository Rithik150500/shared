import uuid
from data_access.models.user import UserNowlez


def test_user_nowlez_table_name():
    assert UserNowlez.__tablename__ == "users_nowlez"


def test_user_nowlez_pk_is_user_id():
    col = UserNowlez.__table__.c["user_id"]
    assert col.primary_key


def test_user_nowlez_referral_code_unique():
    col = UserNowlez.__table__.c["referral_code"]
    assert col.unique is True
    assert col.nullable is True


def test_user_nowlez_default_tier_column_metadata():
    col = UserNowlez.__table__.c["tier"]
    assert col.default.arg == "free"


def test_user_nowlez_has_razorpay_columns():
    assert "razorpay_customer_id" in UserNowlez.__table__.c
    assert "razorpay_subscription_id" in UserNowlez.__table__.c


def test_user_nowlez_referred_by_fk_set_null_on_delete():
    fks = list(UserNowlez.__table__.c["referred_by"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "SET NULL"
    assert fks[0].target_fullname == "users.id"
