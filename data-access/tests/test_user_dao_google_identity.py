"""user_dao Google federated-identity helpers: get_by_google_sub, link_google_identity."""
import uuid

from data_access.daos import user_dao
from data_access.models import UserExternalIdentity


def test_get_by_google_sub_none_when_unlinked(db_session):
    assert user_dao.get_by_google_sub(db_session, "no-such-sub") is None


def test_link_and_resolve_by_google_sub(db_session):
    u, _ = user_dao.get_or_create_by_email(db_session, email="g@example.com")
    user_dao.link_google_identity(db_session, user_id=u.id, google_sub="sub-1", email="g@example.com")
    resolved = user_dao.get_by_google_sub(db_session, "sub-1")
    assert resolved is not None
    assert str(resolved.id) == str(u.id)


def test_link_google_identity_is_idempotent(db_session):
    u, _ = user_dao.get_or_create_by_email(db_session, email="idem@example.com")
    user_dao.link_google_identity(db_session, user_id=u.id, google_sub="sub-idem", email="idem@example.com")
    user_dao.link_google_identity(db_session, user_id=u.id, google_sub="sub-idem", email="idem@example.com")
    rows = db_session.query(UserExternalIdentity).filter_by(provider_sub="sub-idem").count()
    assert rows == 1


def test_second_google_account_on_same_user_is_noop(db_session):
    # The (user_id, provider) unique constraint means a user links ONE Google
    # identity; a second link attempt for the same user is a safe no-op.
    u, _ = user_dao.get_or_create_by_email(db_session, email="one@example.com")
    user_dao.link_google_identity(db_session, user_id=u.id, google_sub="sub-a")
    user_dao.link_google_identity(db_session, user_id=u.id, google_sub="sub-b")
    rows = db_session.query(UserExternalIdentity).filter_by(user_id=u.id).count()
    assert rows == 1
    # The first link wins; the second sub did not get attached.
    assert user_dao.get_by_google_sub(db_session, "sub-a") is not None
    assert user_dao.get_by_google_sub(db_session, "sub-b") is None
