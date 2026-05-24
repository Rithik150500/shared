"""DAO tests for cases.

Uses the SQLite `db_session` fixture from conftest (also works against
Postgres when TEST_DATABASE_URL is set). Models opt into SQLite via the
with_variant(...) pattern; see data_access/models/case.py.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.orm import Session

from data_access.daos import case_dao, case_preferences_dao, user_dao
from ecourts_client.models import Act, Case as DataCase, Party


@pytest.fixture
def test_user_id(db_session: Session) -> uuid.UUID:
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876500001")
    user_dao.ensure_nowlez_extension(db_session, user.id, name="Test User")
    db_session.commit()
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


def test_upsert_creates_row(db_session: Session, test_user_id: uuid.UUID):
    fresh = _case_dataclass("MHCC010054732024")
    row = case_dao.upsert_case(
        db_session, user_id=test_user_id, cnr="MHCC010054732024", case_data=fresh
    )
    db_session.commit()
    assert row.cnr == "MHCC010054732024"
    assert row.title == "Plaintiff vs Defendant"
    assert row.portal == "district"
    assert len(row.parties) == 2


def test_upsert_idempotent(db_session: Session, test_user_id: uuid.UUID):
    fresh = _case_dataclass("MHCC010054732024")
    r1 = case_dao.upsert_case(
        db_session, user_id=test_user_id, cnr="MHCC010054732024", case_data=fresh
    )
    db_session.commit()
    r2 = case_dao.upsert_case(
        db_session, user_id=test_user_id, cnr="MHCC010054732024", case_data=fresh
    )
    db_session.commit()
    assert r1.id == r2.id


def test_diff_and_update_records_change_at(db_session: Session, test_user_id: uuid.UUID):
    fresh1 = _case_dataclass("MHCC010054732024")
    case_dao.upsert_case(
        db_session, user_id=test_user_id, cnr="MHCC010054732024", case_data=fresh1
    )
    db_session.commit()

    fresh2 = DataCase(**{**fresh1.__dict__, "stage": "Disposed"})
    changes = case_dao.diff_and_update(
        db_session, user_id=test_user_id, cnr="MHCC010054732024", case_data=fresh2
    )
    db_session.commit()
    assert "stage" in changes


def test_get_due_for_refresh_orders_nulls_first(db_session: Session, test_user_id: uuid.UUID):
    case_dao.upsert_case(
        db_session,
        user_id=test_user_id,
        cnr="MHCC010054732024",
        case_data=_case_dataclass("MHCC010054732024"),
    )
    db_session.commit()
    due = case_dao.get_due_for_refresh(db_session, limit=10)
    assert any(c.cnr == "MHCC010054732024" for c in due)


def test_mark_cnr_not_found_creates_tombstone(db_session: Session, test_user_id: uuid.UUID):
    case_dao.mark_cnr_not_found(db_session, user_id=test_user_id, cnr="MHCC019999992024")
    db_session.commit()
    row = case_dao.get_by_cnr(db_session, user_id=test_user_id, cnr="MHCC019999992024")
    assert row is not None
    assert row.refresh_enabled is False


def test_delete_case_cascades_to_case_preferences(
    db_session: Session, test_user_id: uuid.UUID,
):
    """A-10 audit fix: delete_case must remove case_preferences too.

    case_preferences has FK only to users (ON DELETE CASCADE) — NOT to cases.
    So Case deletion would orphan preference rows without an explicit delete.
    """
    cnr = "MHCC010054732024"
    # Seed both a Case row and a CasePreferences row for the same (user, cnr).
    case_dao.upsert_case(
        db_session, user_id=test_user_id, cnr=cnr, case_data=_case_dataclass(cnr),
    )
    case_preferences_dao.upsert(
        db_session, user_id=test_user_id, cnr=cnr, alert_level="orders_only",
    )
    db_session.commit()
    assert case_dao.get_by_cnr(db_session, user_id=test_user_id, cnr=cnr) is not None
    assert case_preferences_dao.get_by_cnr(
        db_session, user_id=test_user_id, cnr=cnr,
    ) is not None

    deleted = case_dao.delete_case(db_session, user_id=test_user_id, cnr=cnr)
    db_session.commit()
    assert deleted is True

    # Both rows must be gone — no orphan prefs.
    assert case_dao.get_by_cnr(db_session, user_id=test_user_id, cnr=cnr) is None
    assert case_preferences_dao.get_by_cnr(
        db_session, user_id=test_user_id, cnr=cnr,
    ) is None


def test_delete_case_returns_false_when_absent(
    db_session: Session, test_user_id: uuid.UUID,
):
    """Even with no Case row, the call is a safe no-op (returns False)."""
    assert case_dao.delete_case(
        db_session, user_id=test_user_id, cnr="MHCC019999992024",
    ) is False


def test_delete_case_handles_no_prefs_row(
    db_session: Session, test_user_id: uuid.UUID,
):
    """Case present but no prefs row: still deletes the case, returns True."""
    cnr = "MHCC010054732024"
    case_dao.upsert_case(
        db_session, user_id=test_user_id, cnr=cnr, case_data=_case_dataclass(cnr),
    )
    db_session.commit()
    deleted = case_dao.delete_case(db_session, user_id=test_user_id, cnr=cnr)
    db_session.commit()
    assert deleted is True
    assert case_dao.get_by_cnr(db_session, user_id=test_user_id, cnr=cnr) is None


# ---------------------------------------------------------------------------
# B.5b — Nowlez hook migration DAO methods
# (toggle_refresh / mark_first_ndoh_email_sent / was_first_ndoh_email_sent)
# ---------------------------------------------------------------------------


def _seed_case(db_session: Session, user_id: uuid.UUID, cnr: str = "MHCC010054732024"):
    case_dao.upsert_case(
        db_session, user_id=user_id, cnr=cnr, case_data=_case_dataclass(cnr),
    )
    db_session.commit()


def test_toggle_refresh_flips_flag(db_session: Session, test_user_id: uuid.UUID):
    cnr = "MHCC010054732024"
    _seed_case(db_session, test_user_id, cnr)
    # New cases default to refresh_enabled=True.
    initial = case_dao.get_by_cnr(db_session, user_id=test_user_id, cnr=cnr)
    assert initial.refresh_enabled is True

    new_val = case_dao.toggle_refresh(db_session, user_id=test_user_id, cnr=cnr)
    db_session.commit()
    assert new_val is False
    assert case_dao.get_by_cnr(
        db_session, user_id=test_user_id, cnr=cnr,
    ).refresh_enabled is False


def test_toggle_refresh_returns_new_state(db_session: Session, test_user_id: uuid.UUID):
    cnr = "MHCC010054732024"
    _seed_case(db_session, test_user_id, cnr)
    first = case_dao.toggle_refresh(db_session, user_id=test_user_id, cnr=cnr)
    db_session.commit()
    second = case_dao.toggle_refresh(db_session, user_id=test_user_id, cnr=cnr)
    db_session.commit()
    # First toggle flips True→False, second flips back to True.
    assert first is False
    assert second is True


def test_toggle_refresh_raises_if_case_not_found(
    db_session: Session, test_user_id: uuid.UUID,
):
    with pytest.raises(LookupError):
        case_dao.toggle_refresh(
            db_session, user_id=test_user_id, cnr="MHCC019999992024",
        )


def test_mark_first_ndoh_email_sent_sets_timestamp(
    db_session: Session, test_user_id: uuid.UUID,
):
    cnr = "MHCC010054732024"
    _seed_case(db_session, test_user_id, cnr)
    assert case_dao.get_by_cnr(
        db_session, user_id=test_user_id, cnr=cnr,
    ).first_ndoh_email_sent_at is None

    case_dao.mark_first_ndoh_email_sent(db_session, user_id=test_user_id, cnr=cnr)
    db_session.commit()
    row = case_dao.get_by_cnr(db_session, user_id=test_user_id, cnr=cnr)
    assert row.first_ndoh_email_sent_at is not None


def test_mark_first_ndoh_email_sent_is_idempotent(
    db_session: Session, test_user_id: uuid.UUID,
):
    """Re-stamping should not raise and should overwrite the timestamp."""
    cnr = "MHCC010054732024"
    _seed_case(db_session, test_user_id, cnr)
    case_dao.mark_first_ndoh_email_sent(db_session, user_id=test_user_id, cnr=cnr)
    db_session.commit()
    first_ts = case_dao.get_by_cnr(
        db_session, user_id=test_user_id, cnr=cnr,
    ).first_ndoh_email_sent_at

    # Second call must succeed (idempotent) and not regress to NULL.
    case_dao.mark_first_ndoh_email_sent(db_session, user_id=test_user_id, cnr=cnr)
    db_session.commit()
    second_ts = case_dao.get_by_cnr(
        db_session, user_id=test_user_id, cnr=cnr,
    ).first_ndoh_email_sent_at
    assert second_ts is not None
    # second_ts >= first_ts because we stamp on each call (overwrite semantics).
    assert second_ts >= first_ts


def test_mark_first_ndoh_email_sent_raises_if_case_not_found(
    db_session: Session, test_user_id: uuid.UUID,
):
    with pytest.raises(LookupError):
        case_dao.mark_first_ndoh_email_sent(
            db_session, user_id=test_user_id, cnr="MHCC019999992024",
        )


def test_was_first_ndoh_email_sent_false_when_unset(
    db_session: Session, test_user_id: uuid.UUID,
):
    cnr = "MHCC010054732024"
    _seed_case(db_session, test_user_id, cnr)
    assert case_dao.was_first_ndoh_email_sent(
        db_session, user_id=test_user_id, cnr=cnr,
    ) is False


def test_was_first_ndoh_email_sent_true_after_mark(
    db_session: Session, test_user_id: uuid.UUID,
):
    cnr = "MHCC010054732024"
    _seed_case(db_session, test_user_id, cnr)
    case_dao.mark_first_ndoh_email_sent(db_session, user_id=test_user_id, cnr=cnr)
    db_session.commit()
    assert case_dao.was_first_ndoh_email_sent(
        db_session, user_id=test_user_id, cnr=cnr,
    ) is True


def test_was_first_ndoh_email_sent_false_when_case_missing(
    db_session: Session, test_user_id: uuid.UUID,
):
    """No case row → returns False (used as gating check, not strict lookup)."""
    assert case_dao.was_first_ndoh_email_sent(
        db_session, user_id=test_user_id, cnr="MHCC019999992024",
    ) is False
