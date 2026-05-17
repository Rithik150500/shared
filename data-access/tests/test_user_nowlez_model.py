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
    """Sub-project E (migration 20260601_subproject_e_billing) drops
    the NOT NULL on `tier` so the trial / no-tier state can be NULL,
    and removes the 'free' Python-side default (no default now).
    """
    col = UserNowlez.__table__.c["tier"]
    assert col.nullable is True
    assert col.default is None


def test_user_nowlez_trial_columns_present():
    """Sub-project E migration adds trial_started_at / trial_ends_at."""
    assert "trial_started_at" in UserNowlez.__table__.c
    assert "trial_ends_at" in UserNowlez.__table__.c
    assert UserNowlez.__table__.c["trial_started_at"].nullable is True
    assert UserNowlez.__table__.c["trial_ends_at"].nullable is True


def test_user_nowlez_has_razorpay_columns():
    assert "razorpay_customer_id" in UserNowlez.__table__.c
    assert "razorpay_subscription_id" in UserNowlez.__table__.c


def test_user_nowlez_referred_by_fk_set_null_on_delete():
    fks = list(UserNowlez.__table__.c["referred_by"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "SET NULL"
    assert fks[0].target_fullname == "users.id"
