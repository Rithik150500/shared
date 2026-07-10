"""A-4 audit fix: defense-in-depth CNR regex enforcement at DAO entry.

Handler-level CNR validation (e.g. handlers/monitoring/save_command.py:_CNR_RE)
is the first line of defense, but non-handler write paths (backfill jobs,
scheduler workers, future refactors) bypass it. The DAO must reject
malformed CNRs so we never persist garbage.

The canonical regex is ``^[A-Z]{2}[A-Z0-9]{14}$`` — 16 chars, first 2 alpha
(state), then 14 alphanumeric. The establishment code may begin with digits
(e.g. Madhya Pradesh ``MP20060042872025``), so chars 2:4 need not be letters.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import date

import pytest
sqlite3.register_adapter(uuid.UUID, str)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from data_access.base import Base
from data_access.daos import case_dao, user_dao
from ecourts_client.models import Act, Case as DataCase, Party


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture
def user_id(session) -> uuid.UUID:
    user, _ = user_dao.get_or_create_by_phone(session, phone="+919876500001")
    user_dao.ensure_nowlez_extension(session, user.id, name="Test User")
    session.commit()
    return user.id


def _case_dataclass(cnr: str) -> DataCase:
    return DataCase(
        cnr=cnr,
        title="Plaintiff vs Defendant",
        court="District Court Mumbai",
        stage="Pending",
        next_hearing_date=date(2026, 6, 15),
        judge="Hon. Test",
        parties=[Party(name="Alice", role="petitioner"), Party(name="Bob", role="respondent")],
        acts=[Act(act_name="CPC", section="9")],
    )


# Each bad CNR exercises a different invariant the regex enforces.
@pytest.mark.parametrize("bad_cnr,reason", [
    ("short", "length must be exactly 16"),
    ("ABCD123!", "no special chars + wrong length"),
    ("mhcc010054732024", "must be uppercase"),
    ("", "empty string rejected"),
    ("MHCC01005473202", "15 chars — too short"),
    ("MHCC0100547320244", "17 chars — too long"),
    ("12CC010054732024", "first 2 must be alpha"),
    ("MHCC01005473!024", "special char in tail"),
])
def test_upsert_case_rejects_bad_cnr(session, user_id, bad_cnr, reason):
    fresh = _case_dataclass(bad_cnr or "MHCC010054732024")
    with pytest.raises(ValueError, match="CNR"):
        case_dao.upsert_case(session, user_id=user_id, cnr=bad_cnr, case_data=fresh)


def test_upsert_case_rejects_none_cnr(session, user_id):
    fresh = _case_dataclass("MHCC010054732024")
    with pytest.raises((ValueError, TypeError)):
        case_dao.upsert_case(session, user_id=user_id, cnr=None, case_data=fresh)  # type: ignore[arg-type]


def test_upsert_case_accepts_valid_cnr(session, user_id):
    """Smoke: a canonical CNR still passes."""
    fresh = _case_dataclass("MHCC010054732024")
    row = case_dao.upsert_case(
        session, user_id=user_id, cnr="MHCC010054732024", case_data=fresh,
    )
    assert row.cnr == "MHCC010054732024"


@pytest.mark.parametrize("cnr", [
    "MP20060042872025",  # Madhya Pradesh: numeric establishment ('20') — the
                          # exact CNR that failed before this fix
    "MH12010054732024",  # numeric establishment, valid state (was wrongly
                          # asserted as *rejected* before the fix)
    "PBASB10004672023",  # Punjab special bench: letter inside establishment
])
def test_upsert_case_accepts_numeric_and_mixed_establishment(session, user_id, cnr):
    """Establishment codes may be numeric or mixed, not just alpha."""
    fresh = _case_dataclass(cnr)
    row = case_dao.upsert_case(session, user_id=user_id, cnr=cnr, case_data=fresh)
    assert row.cnr == cnr


@pytest.mark.parametrize("bad_cnr", ["short", "ABCD123!", "mhcc010054732024", ""])
def test_diff_and_update_rejects_bad_cnr(session, user_id, bad_cnr):
    fresh = _case_dataclass(bad_cnr or "MHCC010054732024")
    with pytest.raises(ValueError, match="CNR"):
        case_dao.diff_and_update(session, user_id=user_id, cnr=bad_cnr, case_data=fresh)


@pytest.mark.parametrize("bad_cnr", ["short", "ABCD123!", "mhcc010054732024", ""])
def test_mark_cnr_not_found_rejects_bad_cnr(session, user_id, bad_cnr):
    with pytest.raises(ValueError, match="CNR"):
        case_dao.mark_cnr_not_found(session, user_id=user_id, cnr=bad_cnr)
