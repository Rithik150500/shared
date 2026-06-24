"""Step-4: uploaded_file_dao CRUD + reads + surgical deletes (NULL-cnr carve-out)."""
import uuid
from datetime import datetime, timezone

import pytest

from data_access.daos import uploaded_file_dao
from data_access.models import UploadedFileNowlez, User, Client


def _seed(db_session):
    u = User(email="o@x.com", is_active=True); db_session.add(u); db_session.flush()
    c = Client(id="abc123def456aaaa", user_id=u.id, name="C"); db_session.add(c); db_session.flush()
    return u, c


def test_insert_and_get_by_legacy_id(db_session):
    u, c = _seed(db_session)
    fid = uploaded_file_dao.insert(
        db_session, legacy_sqlite_id=1, client_id=c.id, original_filename="a.pdf",
        file_path="abc123def456aaaa/a.pdf", file_storage="local", cnr=None)
    row = uploaded_file_dao.get_by_legacy_id(db_session, legacy_sqlite_id=1)
    assert row.id == fid and row.original_filename == "a.pdf"


def test_insert_preserves_created_at(db_session):
    # Backfill carries the ORIGINAL upload time (spec §10.1 ISO TEXT -> TIMESTAMPTZ)
    # so the migrated corpus keeps its upload-time ordering instead of collapsing
    # to the backfill instant.
    u, c = _seed(db_session)
    ts = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    uploaded_file_dao.insert(
        db_session, legacy_sqlite_id=10, client_id=c.id, original_filename="old.pdf",
        file_path="abc123def456aaaa/old.pdf", file_storage="local", created_at=ts)
    row = uploaded_file_dao.get_by_legacy_id(db_session, legacy_sqlite_id=10)
    got = row.created_at
    if got.tzinfo is None:  # SQLite test backend stores naive UTC
        got = got.replace(tzinfo=timezone.utc)
    assert got == ts


def test_update_fields_can_correct_created_at(db_session):
    u, c = _seed(db_session)
    uploaded_file_dao.insert(db_session, legacy_sqlite_id=11, client_id=c.id,
                             original_filename="a.pdf", file_path="p", file_storage="local")
    ts = datetime(2023, 6, 7, 8, 9, 10, tzinfo=timezone.utc)
    uploaded_file_dao.update_fields(db_session, legacy_sqlite_id=11, created_at=ts)
    row = uploaded_file_dao.get_by_legacy_id(db_session, legacy_sqlite_id=11)
    got = row.created_at
    if got.tzinfo is None:
        got = got.replace(tzinfo=timezone.utc)
    assert got == ts


def test_update_fields_allowlist_and_reject_unknown(db_session):
    u, c = _seed(db_session)
    uploaded_file_dao.insert(db_session, legacy_sqlite_id=2, client_id=c.id,
                             original_filename="a.pdf", file_path="p", file_storage="local")
    uploaded_file_dao.update_fields(db_session, legacy_sqlite_id=2,
                                    file_storage="r2", r2_object_key="k", preprocessed=True)
    row = uploaded_file_dao.get_by_legacy_id(db_session, legacy_sqlite_id=2)
    assert row.file_storage == "r2" and row.r2_object_key == "k" and row.preprocessed is True
    with pytest.raises(ValueError):
        uploaded_file_dao.update_fields(db_session, legacy_sqlite_id=2, bogus="x")


def test_delete_by_client_cnr_real_cnr_only_null_survives(db_session):
    u, c = _seed(db_session)
    uploaded_file_dao.insert(db_session, legacy_sqlite_id=3, client_id=c.id,
                             original_filename="x.pdf", file_path="p1", file_storage="local", cnr="CNR1")
    uploaded_file_dao.insert(db_session, legacy_sqlite_id=4, client_id=c.id,
                             original_filename="y.pdf", file_path="p2", file_storage="local", cnr=None)
    uploaded_file_dao.delete_by_client_cnr(db_session, client_id=c.id, cnr="CNR1")
    remaining = uploaded_file_dao.list_for_client(db_session, client_id=c.id)
    assert [r.legacy_sqlite_id for r in remaining] == [4]  # NULL-cnr row survives


def test_delete_by_client_cnr_rejects_null(db_session):
    u, c = _seed(db_session)
    with pytest.raises(ValueError):
        uploaded_file_dao.delete_by_client_cnr(db_session, client_id=c.id, cnr=None)


def test_get_by_path_and_recent_for_user(db_session):
    u, c = _seed(db_session)
    uploaded_file_dao.insert(db_session, legacy_sqlite_id=5, client_id=c.id,
                             original_filename="z.pdf", file_path="abc123def456aaaa/z.pdf",
                             file_storage="local")
    assert uploaded_file_dao.get_by_path(db_session, client_id=c.id,
                                         file_path="abc123def456aaaa/z.pdf").legacy_sqlite_id == 5
    assert len(uploaded_file_dao.recent_for_user(db_session, user_id=u.id, n=5)) == 1
