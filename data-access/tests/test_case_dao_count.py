"""Unified per-user active-case count (case_dao.count_active_cases_for_user).

This count is the single source of truth for BOTH Nowlez surfaces' tier caps
(the casepilot web app + the WhatsApp bot), so the free/paid case limits agree
on ONE number across the shared cases table. ``refresh_enabled IS TRUE`` is the
canonical "still tracked" predicate — tombstones (cnr-not-found) and manually
paused/manual rows drop out, matching the bot's long-standing
``_count_active_cases``.

Uses the SQLite ``db_session`` fixture from conftest.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.orm import Session

from data_access.daos import case_dao, user_dao
from ecourts_client.models import Case as DataCase


def _case(cnr: str) -> DataCase:
    return DataCase(
        cnr=cnr,
        title="Plaintiff vs Defendant",
        court="District Court",
        stage="Pending",
        next_hearing_date=date(2026, 6, 15),
        judge="Hon. Test",
    )


@pytest.fixture
def user_id(db_session: Session) -> uuid.UUID:
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876500009")
    db_session.commit()
    return user.id


def test_count_zero_for_new_user(db_session: Session, user_id: uuid.UUID):
    assert case_dao.count_active_cases_for_user(db_session, user_id=user_id) == 0


def test_counts_only_refresh_enabled(db_session: Session, user_id: uuid.UUID):
    # 3 active rows (upsert_case defaults refresh_enabled=True).
    for cnr in ("MHCC010000012024", "MHCC010000022024", "MHCC010000032024"):
        case_dao.upsert_case(db_session, user_id=user_id, cnr=cnr, case_data=_case(cnr))
    # A cnr-not-found tombstone is refresh_enabled=False and must NOT count.
    case_dao.mark_cnr_not_found(db_session, user_id=user_id, cnr="MHCC010000042024")
    db_session.commit()
    assert case_dao.count_active_cases_for_user(db_session, user_id=user_id) == 3


def test_count_is_scoped_per_user(db_session: Session, user_id: uuid.UUID):
    case_dao.upsert_case(
        db_session, user_id=user_id, cnr="MHCC010000012024", case_data=_case("MHCC010000012024")
    )
    other, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876500010")
    case_dao.upsert_case(
        db_session, user_id=other.id, cnr="MHCC010000022024", case_data=_case("MHCC010000022024")
    )
    db_session.commit()
    assert case_dao.count_active_cases_for_user(db_session, user_id=user_id) == 1
    assert case_dao.count_active_cases_for_user(db_session, user_id=other.id) == 1
