"""Step-3: Case gains case_detail_json (JSONB) + case_detail_md +
mini_case_detail_md (TEXT) for the disk->PG blob migration (D1)."""
import uuid

from data_access.models import Case, User


def test_case_detail_blob_roundtrip_sqlite(db_session):
    u = User(email="o@x.com", is_active=True)
    db_session.add(u); db_session.flush()
    c = Case(
        id=uuid.uuid4(), user_id=u.id, cnr="CNR0001", portal="district",
        case_detail_json={"history": [{"date": "2026-01-01"}]},
        case_detail_md="# Body",
        mini_case_detail_md="mini",
    )
    db_session.add(c); db_session.flush()
    got = db_session.get(Case, c.id)
    assert got.case_detail_json == {"history": [{"date": "2026-01-01"}]}
    assert got.case_detail_md == "# Body"
    assert got.mini_case_detail_md == "mini"


def test_case_detail_blobs_nullable(db_session):
    u = User(email="o2@x.com", is_active=True)
    db_session.add(u); db_session.flush()
    c = Case(id=uuid.uuid4(), user_id=u.id, cnr="CNR0002", portal="district")
    db_session.add(c); db_session.flush()
    got = db_session.get(Case, c.id)
    assert got.case_detail_json is None
    assert got.case_detail_md is None
    assert got.mini_case_detail_md is None
