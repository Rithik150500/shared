from datetime import datetime, timedelta, timezone

from data_access.daos import user_dao


def _make_munshi_user(session, phone, *, onboarded=False, last_msg_days_ago=None):
    user, _ = user_dao.get_or_create_by_phone(session, phone=phone, locale="en")
    ext = user_dao.ensure_munshi_extension(session, user.id)
    now = datetime.now(timezone.utc)
    if onboarded:
        ext.onboarded_at = now
    if last_msg_days_ago is not None:
        ext.last_message_at = now - timedelta(days=last_msg_days_ago)
    session.flush()
    return user


def test_count_munshi_users(db_session):
    _make_munshi_user(db_session, "+919000000001")
    _make_munshi_user(db_session, "+919000000002")
    assert user_dao.count_munshi_users(db_session) == 2


def test_count_munshi_onboarded(db_session):
    _make_munshi_user(db_session, "+919000000001", onboarded=True)
    _make_munshi_user(db_session, "+919000000002", onboarded=False)
    assert user_dao.count_munshi_onboarded(db_session) == 1


def test_count_munshi_active_since(db_session):
    _make_munshi_user(db_session, "+919000000001", last_msg_days_ago=2)
    _make_munshi_user(db_session, "+919000000002", last_msg_days_ago=30)
    since = datetime.now(timezone.utc) - timedelta(days=7)
    assert user_dao.count_munshi_active_since(db_session, since) == 1


def test_list_munshi_users_returns_user_and_extension(db_session):
    _make_munshi_user(db_session, "+919000000001")
    rows = user_dao.list_munshi_users(db_session, limit=50, offset=0)
    assert len(rows) == 1
    user, ext = rows[0]
    assert user.phone == "+919000000001"
    assert ext.user_id == user.id


def test_list_munshi_users_search_filters_by_phone(db_session):
    _make_munshi_user(db_session, "+919000000001")
    _make_munshi_user(db_session, "+918111111111")
    rows = user_dao.list_munshi_users(db_session, limit=50, offset=0, search="900000")
    assert len(rows) == 1
    assert rows[0][0].phone == "+919000000001"
