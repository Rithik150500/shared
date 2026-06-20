"""Sub-G step 1: users_nowlez gains the 7 orphan columns that lived only on
the SQLite users table, so identity-channel users have a PG home for them."""
from data_access.models import UserNowlez


def test_model_declares_seven_orphan_columns():
    cols = set(UserNowlez.__table__.columns.keys())
    expected = {
        "monthly_upload_count",
        "usage_reset_date",
        "last_export_at",
        "last_case_exports_at",
        "unsubscribed_at",
        "first_case_email_sent",
        "last_digest_sent_date",
    }
    missing = expected - cols
    assert not missing, f"users_nowlez missing orphan columns: {sorted(missing)}"
