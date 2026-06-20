"""Sanity test that the SQLite db_session fixture is wired and can persist a User."""
import uuid

from data_access.daos import user_dao


def test_db_session_is_sqlite_and_usable(db_session):
    assert db_session.get_bind().dialect.name == "sqlite"
    user, created = user_dao.get_or_create_by_phone(db_session, phone="+919876543210")
    assert created is True
    assert isinstance(user.id, uuid.UUID)
    db_session.commit()
    assert user_dao.get_by_phone(db_session, "+919876543210") is not None
