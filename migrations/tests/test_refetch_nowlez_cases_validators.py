"""Unit tests for the re-fetch validators.

Uses the same in-memory SQLite ``db_session`` + autouse ``get_session``
monkeypatch as ``test_refetch_nowlez_cases.py`` (see conftest.py).
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from data_access.daos import case_dao, user_dao
from ecourts_client.models import Case as DataCase, Party

from migrations.refetch_nowlez_cases_validators import (
    assert_fk_integrity,
    row_counts_match,
    sample_diff,
)


def _seed_legacy_row(s: Session, legacy_id: int, user_id, cnr: str) -> None:
    s.execute(
        text(
            "INSERT INTO _legacy_nowlez_client_cases "
            "(id, user_id, cnr, client_id, refresh_enabled, notes) "
            "VALUES (:id, :u, :cnr, NULL, 1, NULL)"
        ),
        {"id": legacy_id, "u": str(user_id), "cnr": cnr},
    )


def _make_case(s: Session, user_id, cnr: str) -> None:
    case_dao.upsert_case(
        s,
        user_id=user_id,
        cnr=cnr,
        case_data=DataCase(
            cnr=cnr,
            title=f"Title-{cnr}",
            court="Test Court",
            stage="Pending",
            next_hearing_date=date(2026, 6, 1),
            judge="J",
            parties=[Party(name="P", role="petitioner")],
        ),
    )


def test_row_counts_match_returns_both_counts(db_session: Session):
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876600101")
    db_session.commit()
    _seed_legacy_row(db_session, 1, user.id, "MHCC010054732024")
    _seed_legacy_row(db_session, 2, user.id, "MHCC010054742024")
    _make_case(db_session, user.id, "MHCC010054732024")
    db_session.commit()

    legacy, new = row_counts_match()

    assert legacy == 2
    assert new == 1


def test_sample_diff_finds_missing_rows(db_session: Session):
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876600102")
    db_session.commit()
    # Two legacy rows; only one migrated.
    _seed_legacy_row(db_session, 1, user.id, "MHCC010054732024")
    _seed_legacy_row(db_session, 2, user.id, "MHCC010054742024")
    _make_case(db_session, user.id, "MHCC010054732024")
    db_session.commit()

    problems = sample_diff(sample_size=10)

    # Exactly one row missing.
    assert len(problems) == 1
    assert problems[0]["cnr"] == "MHCC010054742024"
    assert problems[0]["reason"] == "missing"


def test_sample_diff_empty_when_all_present(db_session: Session):
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876600103")
    db_session.commit()
    _seed_legacy_row(db_session, 1, user.id, "MHCC010054732024")
    _make_case(db_session, user.id, "MHCC010054732024")
    db_session.commit()

    assert sample_diff(sample_size=10) == []


def test_assert_fk_integrity_passes_when_all_users_present(db_session: Session):
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876600104")
    db_session.commit()
    _make_case(db_session, user.id, "MHCC010054732024")
    db_session.commit()

    # Should not raise.
    assert_fk_integrity()


def test_assert_fk_integrity_raises_when_orphan_present(db_session: Session):
    """Detect orphan: case.user_id points at a deleted user.

    SQLite's FK enforcement is off by default, so the orphan insert below
    succeeds in the fixture's in-memory DB even though Postgres would have
    cascaded the cases row away. That's exactly the failure mode the
    validator is designed to catch in prod (e.g. someone hard-deletes a
    user row out-of-band).
    """
    import uuid

    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876600105")
    db_session.commit()
    _make_case(db_session, user.id, "MHCC010054732024")
    # Forge an orphan: change the user_id to a UUID that doesn't exist.
    orphan_user_id = uuid.uuid4()
    db_session.execute(
        text("UPDATE cases SET user_id = :u WHERE cnr = :cnr"),
        {"u": str(orphan_user_id), "cnr": "MHCC010054732024"},
    )
    db_session.commit()

    with pytest.raises(AssertionError, match="orphan cases"):
        assert_fk_integrity()
