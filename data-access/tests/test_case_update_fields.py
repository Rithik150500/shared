"""Step-3: case_dao.update_fields applies a partial column patch over an
explicit allow-list (incl. the detail blobs + notes), scoped to (user_id, cnr)."""
import uuid

import pytest

from data_access.daos import case_dao
from data_access.models import Case, User


def _seed(db_session):
    u = User(email="o@x.com", is_active=True)
    db_session.add(u); db_session.flush()
    c = Case(id=uuid.uuid4(), user_id=u.id, cnr="DLCT010000012026", portal="district")
    db_session.add(c); db_session.flush()
    return u, c


def test_update_fields_patches_allowed_columns(db_session):
    u, c = _seed(db_session)
    changed = case_dao.update_fields(
        db_session, user_id=u.id, cnr="DLCT010000012026",
        case_status="Disposed", notes="hi",
        case_detail_md="# Body", mini_case_detail_md="mini",
        case_detail_json={"history": [{"date": "2026-01-01"}]},
    )
    got = db_session.get(Case, c.id)
    assert got.case_status == "Disposed"
    assert got.notes == "hi"
    assert got.case_detail_md == "# Body"
    assert got.mini_case_detail_md == "mini"
    assert got.case_detail_json == {"history": [{"date": "2026-01-01"}]}
    assert set(changed) >= {"case_status", "notes", "case_detail_md",
                            "mini_case_detail_md", "case_detail_json"}


def test_update_fields_rejects_unknown_column(db_session):
    u, c = _seed(db_session)
    with pytest.raises(ValueError):
        case_dao.update_fields(db_session, user_id=u.id, cnr="DLCT010000012026",
                               bogus_col="x")


def test_update_fields_missing_row_returns_empty(db_session):
    u, _ = _seed(db_session)
    assert case_dao.update_fields(db_session, user_id=u.id,
                                  cnr="DLCT010000999026", notes="x") == []


def test_update_fields_bumps_last_change_at_for_diffable(db_session):
    u, c = _seed(db_session)
    case_dao.update_fields(db_session, user_id=u.id, cnr="DLCT010000012026",
                           case_status="Disposed")
    assert db_session.get(Case, c.id).last_change_at is not None
