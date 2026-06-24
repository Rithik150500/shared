"""Step-4: notification_dao — dedup upsert, user-scoped reads, surgical deletes."""
import uuid

from data_access.daos import notification_dao
from data_access.models import User, Client


def _seed(db_session):
    u = User(email="o@x.com", is_active=True); db_session.add(u); db_session.flush()
    c = Client(id="abc123def456aaaa", user_id=u.id, name="C"); db_session.add(c); db_session.flush()
    return u, c


def test_upsert_dedup_key_collapses(db_session):
    u, c = _seed(db_session)
    notification_dao.upsert(db_session, legacy_sqlite_id=1, client_id=c.id,
                            user_id=u.id, dedup_key="k1", case_cnr="CNR1",
                            type="new_orders", title="t", message="m")
    notification_dao.upsert(db_session, legacy_sqlite_id=2, client_id=c.id,
                            user_id=u.id, dedup_key="k1", case_cnr="CNR1",
                            type="new_orders", title="t2", message="m2")
    assert notification_dao.count_unread(db_session, user_id=u.id) == 1  # collapsed


def test_null_dedup_key_does_not_collapse(db_session):
    u, c = _seed(db_session)
    for i in (3, 4):
        notification_dao.upsert(db_session, legacy_sqlite_id=i, client_id=c.id,
                                user_id=u.id, dedup_key=None, case_cnr=None,
                                type="refresh_error", title="t", message="m")
    assert notification_dao.count_unread(db_session, user_id=u.id) == 2


def test_user_scoped_reads_and_mark(db_session):
    u, c = _seed(db_session)
    notification_dao.upsert(db_session, legacy_sqlite_id=5, client_id=c.id,
                            user_id=u.id, dedup_key="k5", case_cnr=None,
                            type="new_orders", title="t", message="m")
    assert notification_dao.verify_ownership(db_session, legacy_sqlite_id=5, user_id=u.id) is True
    other = uuid.uuid4()
    assert notification_dao.verify_ownership(db_session, legacy_sqlite_id=5, user_id=other) is False
    notification_dao.mark_all_read(db_session, user_id=u.id)
    assert notification_dao.count_unread(db_session, user_id=u.id) == 0


def test_delete_by_client_cnr_null_survives(db_session):
    u, c = _seed(db_session)
    notification_dao.upsert(db_session, legacy_sqlite_id=6, client_id=c.id,
                            user_id=u.id, dedup_key="k6", case_cnr="CNR1",
                            type="x", title="t", message="m")
    notification_dao.upsert(db_session, legacy_sqlite_id=7, client_id=c.id,
                            user_id=u.id, dedup_key="k7", case_cnr=None,
                            type="x", title="t", message="m")
    notification_dao.delete_by_client_cnr(db_session, client_id=c.id, case_cnr="CNR1")
    assert notification_dao.count_unread(db_session, user_id=u.id) == 1  # NULL-cnr survives


def test_list_for_case_scheduler_read(db_session):
    u, c = _seed(db_session)
    notification_dao.upsert(db_session, legacy_sqlite_id=8, client_id=c.id,
                            user_id=u.id, dedup_key="k8", case_cnr="CNRX",
                            type="hearing_reminder_7d", title="Hearing 2026-07-01",
                            message="m")
    rows = notification_dao.list_for_case(db_session, client_id=c.id, case_cnr="CNRX")
    assert any("2026-07-01" in r.title for r in rows)
