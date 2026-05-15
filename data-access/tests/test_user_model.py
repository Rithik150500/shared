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


def test_user_defaults():
    u = User(phone="+919876543210")
    assert u.locale == "en"
    assert u.timezone == "Asia/Kolkata"
    assert u.is_active is True
