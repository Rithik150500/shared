"""Phase 2: create_manual_case source/refresh_enabled params + get_due_for_refresh
including non-eCourts auto rows (cnr=NULL) after the relax."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from data_access.daos import case_dao, user_dao


@pytest.fixture
def uid(db_session: Session) -> uuid.UUID:
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876500099")
    user_dao.ensure_nowlez_extension(db_session, user.id, name="Consumer User")
    db_session.commit()
    return user.id


def test_create_manual_case_defaults_to_manual(db_session, uid):
    row = case_dao.create_manual_case(
        db_session, user_id=uid, forum="arbitration",
        forum_case_ref="ARB-1", title="X vs Y",
    )
    db_session.commit()
    assert row.source == "manual"
    assert row.refresh_enabled is False


def test_create_manual_case_auto_source_and_refresh(db_session, uid):
    row = case_dao.create_manual_case(
        db_session, user_id=uid, forum="consumer", forum_case_ref="SC/1/2024",
        title="A vs B", source="ejagriti_auto", refresh_enabled=True,
        case_detail_json={"ejagriti_commission_id": 11290525},
    )
    db_session.commit()
    assert row.source == "ejagriti_auto"
    assert row.refresh_enabled is True
    assert row.cnr is None


def test_get_due_includes_consumer_auto_row_excludes_manual(db_session, uid):
    # consumer auto row: cnr=NULL, source=ejagriti_auto, refresh_enabled=True
    case_dao.create_manual_case(
        db_session, user_id=uid, forum="consumer", forum_case_ref="SC/2/2024",
        title="Auto", source="ejagriti_auto", refresh_enabled=True,
    )
    # a manual (non-auto) row must NOT be polled
    case_dao.create_manual_case(
        db_session, user_id=uid, forum="arbitration", forum_case_ref="ARB-2",
        title="Manual",
    )
    db_session.commit()

    refs = {c.forum_case_ref for c in case_dao.get_due_for_refresh(db_session, limit=100)}
    assert "SC/2/2024" in refs   # consumer auto row IS polled despite cnr=NULL
    assert "ARB-2" not in refs   # manual row excluded (source='manual')
