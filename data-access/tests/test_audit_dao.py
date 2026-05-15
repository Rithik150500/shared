import uuid

import pytest
import sqlalchemy.exc

from data_access.daos import audit_dao, user_dao
from data_access.models import AuditLog


def test_log_event(postgresql_session):
    u, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    a = audit_dao.log_event(
        postgresql_session,
        event_type="user.login.otp",
        user_id=u.id,
        source="identity",
        metadata={"channel": "whatsapp"},
        ip_address="203.0.113.5",
    )
    assert a.id is not None
    rows = postgresql_session.query(AuditLog).filter_by(user_id=u.id).all()
    assert len(rows) == 1
    assert rows[0].event_type == "user.login.otp"
    assert rows[0].metadata_ == {"channel": "whatsapp"}
    assert rows[0].source == "identity"
    assert rows[0].ip_address is not None


def test_log_event_without_user_id(postgresql_session):
    # System-level events have no user_id
    a = audit_dao.log_event(
        postgresql_session,
        event_type="system.startup",
        source="system",
    )
    assert a.user_id is None
    assert a.actor_id is None
    assert a.metadata_ == {}


def test_invalid_source_rejected(postgresql_session):
    # CHECK constraint should reject 'invalid' source
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        audit_dao.log_event(postgresql_session, event_type="x", source="invalid", metadata={})
        postgresql_session.flush()
    # Roll back the broken transaction so fixture teardown's commit() doesn't fail.
    postgresql_session.rollback()


def test_log_event_with_actor_id(postgresql_session):
    # Admin action: actor_id is the admin who did it; user_id is the affected user
    admin, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919999999999")
    target, _ = user_dao.get_or_create_by_phone(postgresql_session, phone="+919876543210")
    a = audit_dao.log_event(
        postgresql_session,
        event_type="user.disabled",
        user_id=target.id,
        actor_id=admin.id,
        source="nowlez",
    )
    assert a.user_id == target.id
    assert a.actor_id == admin.id
