import uuid
from datetime import datetime, timezone
from data_access.base import Base
from data_access.models.user import User


def test_user_table_name():
    assert User.__tablename__ == "users"


def test_user_has_uuid_id_column():
    col = User.__table__.c["id"]
    assert col.primary_key
    assert col.type.python_type == uuid.UUID


def test_user_phone_unique_nullable():
    col = User.__table__.c["phone"]
    assert col.unique is True
    assert col.nullable is True


def test_user_defaults_declared_on_columns():
    # Defaults fire at flush time (not __init__), so assert column metadata
    # — consistent with UserMunshi/UserNowlez tests.
    assert User.__table__.c["locale"].default.arg == "en"
    assert User.__table__.c["timezone"].default.arg == "Asia/Kolkata"
    assert User.__table__.c["is_active"].default.arg is True
