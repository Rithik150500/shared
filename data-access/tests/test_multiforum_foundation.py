"""Phase-1A multi-forum foundation: new schema columns + DAO generalization.

Runs on the SQLite ``db_session`` fixture (also valid against Postgres when
TEST_DATABASE_URL is set). Verifies eCourts writes keep populating the new
forum/forum_case_ref/source columns consistently, and exercises the new
manual (non-eCourts) case path plus the forum-aware refresh gating.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.orm import Session

from data_access.daos import case_dao, user_dao
from ecourts_client.models import Case as DataCase, Party


@pytest.fixture
def test_user_id(db_session: Session) -> uuid.UUID:
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876500042")
    user_dao.ensure_nowlez_extension(db_session, user.id, name="Multiforum User")
    db_session.commit()
    return user.id


def _ecourts_case(cnr: str) -> DataCase:
    return DataCase(
        cnr=cnr,
        title="Plaintiff vs Defendant",
        court="District Court Mumbai",
        stage="Pending",
        next_hearing_date=date(2026, 6, 15),
        judge="Hon. Test",
        parties=[Party(name="Alice", role="petitioner")],
    )


# --- eCourts writes populate the new columns consistently ----------------

def test_ecourts_upsert_sets_forum_columns(db_session, test_user_id):
    row = case_dao.upsert_case(
        db_session, user_id=test_user_id, cnr="MHCC010054732024",
        case_data=_ecourts_case("MHCC010054732024"),
    )
    db_session.commit()
    assert row.forum == "ecourts_district"
    assert row.source == "ecourts_auto"
    assert row.forum_case_ref == "MHCC010054732024"
    assert row.portal == "district"


def test_ecourts_highcourt_forum(db_session, test_user_id):
    row = case_dao.upsert_case(
        db_session, user_id=test_user_id, cnr="DLHC010012342024",
        case_data=_ecourts_case("DLHC010012342024"),
    )
    db_session.commit()
    assert row.forum == "ecourts_highcourt"
    assert row.portal == "highcourt"


# --- Manual (non-eCourts) case path --------------------------------------

def test_create_manual_case_synthetic_ref(db_session, test_user_id):
    row = case_dao.create_manual_case(
        db_session, user_id=test_user_id, forum="arbitration",
        title="ACME vs Beta (Arbitration)", court="Sole Arbitrator, Mumbai",
    )
    db_session.commit()
    assert row.cnr is None
    assert row.portal is None
    assert row.source == "manual"
    assert row.refresh_enabled is False
    assert row.forum == "arbitration"
    assert row.forum_case_ref.startswith("m-")
    # case_number falls back to the ref so the card/timeline have a label.
    assert row.case_number == row.forum_case_ref


def test_create_manual_case_explicit_ref_and_detail(db_session, test_user_id):
    detail = {
        "filing_date": "2026-03-01",
        "next_hearing_date": "2026-08-12",
        "hearing_history": [
            {"hearing_date": "2026-04-10", "purpose": "Evidence", "judge": "Member A"},
        ],
        "parties": [{"name": "Consumer X", "type": "complainant"}],
    }
    row = case_dao.create_manual_case(
        db_session, user_id=test_user_id, forum="consumer",
        forum_case_ref="CC/512/2024", title="X vs Bank",
        court="NCDRC", case_detail_json=detail,
    )
    db_session.commit()
    assert row.forum_case_ref == "CC/512/2024"
    assert row.case_number == "CC/512/2024"
    assert row.case_detail_json["next_hearing_date"] == "2026-08-12"
    assert len(row.history) == 1
    assert len(row.parties) == 1


def test_create_manual_case_rejects_ecourts_forum(db_session, test_user_id):
    with pytest.raises(ValueError):
        case_dao.create_manual_case(
            db_session, user_id=test_user_id, forum="ecourts_district",
            forum_case_ref="X",
        )


def test_get_by_ref_roundtrip(db_session, test_user_id):
    row = case_dao.create_manual_case(
        db_session, user_id=test_user_id, forum="drt",
        forum_case_ref="OA/77/2025", title="Bank vs Debtor", court="DRT-1 Delhi",
    )
    db_session.commit()
    found = case_dao.get_by_ref(
        db_session, user_id=test_user_id, forum="drt", forum_case_ref="OA/77/2025",
    )
    assert found is not None and found.id == row.id


def test_update_fields_by_ref(db_session, test_user_id):
    case_dao.create_manual_case(
        db_session, user_id=test_user_id, forum="supreme_court",
        forum_case_ref="SLP(C) 12345/2026", title="A vs Union of India",
    )
    db_session.commit()
    changed = case_dao.update_fields_by_ref(
        db_session, user_id=test_user_id, forum="supreme_court",
        forum_case_ref="SLP(C) 12345/2026", stage="Admitted",
    )
    db_session.commit()
    assert "stage" in changed


def test_delete_case_by_ref(db_session, test_user_id):
    case_dao.create_manual_case(
        db_session, user_id=test_user_id, forum="arbitration",
        forum_case_ref="ARB-2026-1", title="M vs N",
    )
    db_session.commit()
    assert case_dao.delete_case_by_ref(
        db_session, user_id=test_user_id, forum="arbitration",
        forum_case_ref="ARB-2026-1",
    ) is True
    assert case_dao.get_by_ref(
        db_session, user_id=test_user_id, forum="arbitration",
        forum_case_ref="ARB-2026-1",
    ) is None


# --- Refresh gating: manual rows are never returned ----------------------

def test_get_due_for_refresh_excludes_manual(db_session, test_user_id):
    case_dao.upsert_case(
        db_session, user_id=test_user_id, cnr="MHCC010054732024",
        case_data=_ecourts_case("MHCC010054732024"),
    )
    case_dao.create_manual_case(
        db_session, user_id=test_user_id, forum="consumer",
        forum_case_ref="CC/1/2026", title="Manual",
    )
    db_session.commit()
    due = case_dao.get_due_for_refresh(db_session, limit=50)
    forums = {c.forum for c in due}
    assert "ecourts_district" in forums
    assert "consumer" not in forums
    assert all(c.cnr is not None for c in due)


# --- Identity validation --------------------------------------------------

def test_assert_valid_ref_rules():
    # eCourts: requires a valid CNR and ref == cnr.
    case_dao._assert_valid_ref(
        "ecourts_district", cnr="MHCC010054732024",
        forum_case_ref="MHCC010054732024",
    )
    with pytest.raises(ValueError):
        case_dao._assert_valid_ref("ecourts_district", cnr=None, forum_case_ref="X")
    with pytest.raises(ValueError):  # eCourts ref must equal cnr
        case_dao._assert_valid_ref(
            "ecourts_district", cnr="MHCC010054732024", forum_case_ref="other",
        )
    with pytest.raises(ValueError):  # non-eCourts must not carry a CNR
        case_dao._assert_valid_ref(
            "consumer", cnr="MHCC010054732024", forum_case_ref="MHCC010054732024",
        )
    # non-eCourts free-form ref ok (/, -, . and spaces allowed).
    case_dao._assert_valid_ref("consumer", cnr=None, forum_case_ref="CC/512/2024")


# --- Uniqueness / upsert semantics ---------------------------------------

def test_create_manual_case_same_ref_upserts_in_place(db_session, test_user_id):
    case_dao.create_manual_case(
        db_session, user_id=test_user_id, forum="consumer",
        forum_case_ref="CC/9/2026", title="First",
    )
    db_session.commit()
    case_dao.create_manual_case(
        db_session, user_id=test_user_id, forum="consumer",
        forum_case_ref="CC/9/2026", title="Second",
    )
    db_session.commit()
    rows = [
        c for c in case_dao.list_by_user(db_session, user_id=test_user_id)
        if c.forum == "consumer"
    ]
    assert len(rows) == 1
    assert rows[0].title == "Second"
