import uuid
from data_access.models.user import UserMunshi


def test_user_munshi_table_name():
    assert UserMunshi.__tablename__ == "users_munshi"


def test_user_munshi_pk_is_user_id():
    col = UserMunshi.__table__.c["user_id"]
    assert col.primary_key
    assert col.type.python_type == uuid.UUID


def test_user_munshi_fk_cascade():
    fks = list(UserMunshi.__table__.c["user_id"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"
    assert fks[0].target_fullname == "users.id"


def test_user_munshi_re_engage_opted_out_defaults_false():
    # Server-side default declared as 'false' (Python-side default may not fire at __init__)
    from sqlalchemy import text
    col = UserMunshi.__table__.c["re_engage_opted_out"]
    assert col.default.arg is False
