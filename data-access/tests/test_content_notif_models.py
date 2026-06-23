"""Step-4: the 3 content/notif models round-trip on the SQLite test variant.
tsvector/GIN are PG-only (migration); models declare nullable search_tsv so
SQLite create_all works."""
import uuid

from data_access.models import (
    UploadedFileNowlez, ChatHistoryNowlez, NotificationNowlez, User, Client,
)


def _owner_and_client(db_session):
    u = User(email="o@x.com", is_active=True)
    db_session.add(u); db_session.flush()
    c = Client(id="abc123def456aaaa", user_id=u.id, name="C")
    db_session.add(c); db_session.flush()
    return u, c


def test_uploaded_file_roundtrip(db_session):
    u, c = _owner_and_client(db_session)
    f = UploadedFileNowlez(
        legacy_sqlite_id=11, client_id=c.id, original_filename="a.pdf",
        descriptive_name="Order", summary="s", page_count=3, cnr="CNR1",
        file_path="abc123def456aaaa/CNR1/a.pdf", file_storage="local",
    )
    db_session.add(f); db_session.flush()
    got = db_session.get(UploadedFileNowlez, f.id)
    assert got.legacy_sqlite_id == 11 and got.file_storage == "local"
    assert got.r2_object_key is None and got.preprocessed is False


def test_chat_history_allows_synthetic_client_id_no_fk(db_session):
    # No FK on client_id: the unified synthetic key (no clients row) must insert.
    h = ChatHistoryNowlez(
        legacy_sqlite_id=5, client_id="__unified__abc123def456aaaa",
        role="user", content="hello", sources_json=None, function_calls_json=None,
    )
    db_session.add(h); db_session.flush()
    assert db_session.get(ChatHistoryNowlez, h.id).client_id.startswith("__unified__")


def test_notification_roundtrip_with_denormalized_user_id(db_session):
    u, c = _owner_and_client(db_session)
    n = NotificationNowlez(
        legacy_sqlite_id=7, client_id=c.id, user_id=u.id, case_cnr="CNR1",
        type="new_orders", title="t", message="m", dedup_key="k1",
    )
    db_session.add(n); db_session.flush()
    got = db_session.get(NotificationNowlez, n.id)
    assert got.user_id == u.id and got.is_read is False and got.dedup_key == "k1"
