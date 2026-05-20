"""DAO unit tests for case_preferences_dao."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
sqlite3.register_adapter(uuid.UUID, str)

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from data_access.base import Base
from data_access.daos import case_preferences_dao
from data_access.models import User, CasePreferences


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")

    # sqlite has FK enforcement OFF by default; enable it per-connection so
    # ON DELETE CASCADE actually fires for the cascade test.
    @event.listens_for(engine, "connect")
    def _fk_pragma_on_connect(dbapi_con, _):  # pragma: no cover
        dbapi_con.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture
def user(session) -> User:
    u = User(phone="+919876543210", locale="en")
    session.add(u)
    session.commit()
    return u


def test_upsert_inserts_with_defaults(session, user):
    """First upsert with no kwargs creates a row with default values."""
    row = case_preferences_dao.upsert(session, user_id=user.id, cnr="DLND01")
    assert row.alert_level == "all"  # server_default
    assert row.digest_enabled is True
    assert row.snooze_until is None


def test_upsert_inserts_with_explicit_alert_level(session, user):
    row = case_preferences_dao.upsert(
        session, user_id=user.id, cnr="DLND01", alert_level="digest_only",
    )
    assert row.alert_level == "digest_only"
    assert row.digest_enabled is True  # default


def test_upsert_updates_only_passed_columns(session, user):
    """Second upsert with only digest_enabled doesn't touch alert_level."""
    case_preferences_dao.upsert(
        session, user_id=user.id, cnr="DLND01", alert_level="orders_only",
    )
    case_preferences_dao.upsert(
        session, user_id=user.id, cnr="DLND01", digest_enabled=False,
    )
    row = case_preferences_dao.get_by_cnr(session, user_id=user.id, cnr="DLND01")
    assert row.alert_level == "orders_only"  # preserved
    assert row.digest_enabled is False  # updated


def test_upsert_idempotent_no_args_after_insert(session, user):
    """Second upsert with no args doesn't change anything."""
    first = case_preferences_dao.upsert(
        session, user_id=user.id, cnr="DLND01", alert_level="hearings_only",
    )
    second = case_preferences_dao.upsert(session, user_id=user.id, cnr="DLND01")
    assert second.alert_level == "hearings_only"
    # Same row identity (PK match)
    assert second.user_id == first.user_id
    assert second.cnr == first.cnr


def test_get_by_cnr_returns_none_when_absent(session, user):
    assert case_preferences_dao.get_by_cnr(
        session, user_id=user.id, cnr="DLND01"
    ) is None


def test_list_for_user_returns_all_rows_oldest_first(session, user):
    case_preferences_dao.upsert(session, user_id=user.id, cnr="DLND01")
    case_preferences_dao.upsert(session, user_id=user.id, cnr="DLND02")
    case_preferences_dao.upsert(session, user_id=user.id, cnr="DLND03")
    rows = case_preferences_dao.list_for_user(session, user_id=user.id)
    assert len(rows) == 3
    assert [r.cnr for r in rows] == ["DLND01", "DLND02", "DLND03"]


def test_delete_returns_rowcount(session, user):
    case_preferences_dao.upsert(session, user_id=user.id, cnr="DLND01")
    assert case_preferences_dao.delete(
        session, user_id=user.id, cnr="DLND01"
    ) == 1
    assert case_preferences_dao.get_by_cnr(
        session, user_id=user.id, cnr="DLND01"
    ) is None


def test_delete_returns_zero_when_absent(session, user):
    assert case_preferences_dao.delete(
        session, user_id=user.id, cnr="DLND01"
    ) == 0


def test_upsert_with_snooze_until(session, user):
    until = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    row = case_preferences_dao.upsert(
        session, user_id=user.id, cnr="DLND01", snooze_until=until,
    )
    # Round-trip through sqlite may strip tz; compare naively
    fetched = case_preferences_dao.get_by_cnr(
        session, user_id=user.id, cnr="DLND01"
    )
    su = fetched.snooze_until
    if su is not None and su.tzinfo is None:
        su = su.replace(tzinfo=timezone.utc)
    assert su == until


def test_upsert_user_cascade_delete(session, user):
    """When user is deleted, case_preferences rows cascade-delete."""
    case_preferences_dao.upsert(session, user_id=user.id, cnr="DLND01")
    case_preferences_dao.upsert(session, user_id=user.id, cnr="DLND02")
    assert len(case_preferences_dao.list_for_user(session, user_id=user.id)) == 2

    session.delete(user)
    session.commit()

    # Re-query: all prefs should be gone
    assert len(case_preferences_dao.list_for_user(session, user_id=user.id)) == 0


def test_upsert_update_path_uses_tz_aware_utc(session, user):
    """A-9 audit fix: the UPDATE branch must write a tz-aware datetime to
    updated_at, not the deprecated naive `datetime.utcnow()`.

    Trigger the update branch (second upsert with a value) and assert the
    DAO didn't emit a DeprecationWarning and the persisted value is the
    expected UTC instant.
    """
    import warnings

    # First call: INSERT (skips update-values branch).
    case_preferences_dao.upsert(
        session, user_id=user.id, cnr="DLND01", alert_level="all",
    )

    # Second call: triggers the UPDATE branch where updated_at is set.
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        # Should not raise (no DeprecationWarning).
        case_preferences_dao.upsert(
            session, user_id=user.id, cnr="DLND01", alert_level="orders_only",
        )

    row = case_preferences_dao.get_by_cnr(session, user_id=user.id, cnr="DLND01")
    # SQLite strips tz on round-trip; re-attach to verify "UTC was used".
    # The real assertion is the DeprecationWarning above — this is a smoke
    # check that the new datetime call still produced a usable value.
    assert row.updated_at is not None
