"""Tests for ``migrations.backfill_legacy_users`` (Sub-project A R2).

Source is a legacy SQLite *file* (the casepilot ``app.db`` shape); destination
is the unified Postgres ``users`` + ``users_nowlez``. The in-memory
``db_session`` fixture (data-access models, see this dir's conftest) stands in
for Postgres — same pattern as ``test_cutover_subproject_e``.

Covers: net-new insert, idempotency (already-linked), self-heal of an existing
user missing a Nowlez extension, linking an extension that lacks a legacy id,
the genuine email-conflict case, password carry-over, the pattern denylist, the
manual-drop list, the SQL ⊆ is_test_user invariant, dry-run, and the fail-loud
candidate-count band.
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest
from sqlalchemy import select

from data_access.models.user import User, UserNowlez
from migrations.backfill_legacy_users import (
    EXCLUDE_TEST_SQL,
    EXPECTED_MIN_CANDIDATES,
    _MANUAL_DROP_EMAILS,
    assert_candidate_count,
    backfill,
    is_test_user,
)


# Legacy casepilot SQLite shape (subset of columns the backfill reads).
_LEGACY_DDL = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    last_login TEXT,
    tier TEXT NOT NULL DEFAULT 'free'
)
"""


def _row(**over):
    base = dict(
        id=uuid.uuid4().hex[:8],
        name="Test User",
        email=f"{uuid.uuid4().hex[:8]}@rsrlegal.in",  # real-looking domain
        password_hash="hash$abc",
        created_at="2026-04-01 10:00:00",
        is_admin=0,
        is_active=1,
        last_login=None,
        tier="free",
    )
    base.update(over)
    return base


def _legacy_db(tmp_path, rows):
    """Write a legacy-shape SQLite file with ``rows`` and return its path."""
    p = tmp_path / "app.db"
    conn = sqlite3.connect(p)
    conn.execute(_LEGACY_DDL)
    conn.executemany(
        "INSERT INTO users "
        "(id,name,email,password_hash,created_at,is_admin,is_active,last_login,tier) "
        "VALUES (:id,:name,:email,:password_hash,:created_at,:is_admin,:is_active,:last_login,:tier)",
        rows,
    )
    conn.commit()
    conn.close()
    return str(p)


def test_net_new_insert_creates_users_and_users_nowlez(tmp_path, db_session):
    path = _legacy_db(tmp_path, [_row(
        id="leg_abc", name="Priya", email="priya@rsrlegal.in",
        password_hash="pw$priya", is_admin=1, tier="advocate",
        last_login="2026-05-01 09:30:00",
    )])
    summary = backfill(sqlite_path=path, pg_session=db_session, enforce_count_band=False)

    assert summary["inserted"] == 1
    u = db_session.execute(
        select(User).where(User.email == "priya@rsrlegal.in")
    ).scalar_one()
    assert u.phone is None                  # email-primary backfilled user
    assert u.password_hash == "pw$priya"    # login preserved
    assert u.last_login_at is not None

    n = db_session.execute(
        select(UserNowlez).where(UserNowlez.user_id == u.id)
    ).scalar_one()
    assert n.legacy_sqlite_id == "leg_abc"
    assert n.name == "Priya"
    assert n.is_admin is True
    assert n.tier == "advocate"


def test_already_linked_is_idempotent(tmp_path, db_session):
    path = _legacy_db(tmp_path, [_row(id="leg_idem", email="idem@law.in")])
    first = backfill(sqlite_path=path, pg_session=db_session, enforce_count_band=False)
    second = backfill(sqlite_path=path, pg_session=db_session, enforce_count_band=False)

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["already_linked"] == 1
    rows = db_session.execute(select(User).where(User.email == "idem@law.in")).all()
    assert len(rows) == 1  # not duplicated


def test_heals_existing_user_missing_nowlez(tmp_path, db_session):
    """A real user exists (e.g. phone-primary who set an email) but has NO
    Nowlez extension. Backfill must COMPLETE them, not skip forever."""
    existing = User(email="merge@law.in", phone="+91900111", password_hash="pw")
    db_session.add(existing)
    db_session.flush()
    eid = existing.id

    path = _legacy_db(tmp_path, [_row(id="leg_merge", name="Meera",
                                      email="merge@law.in", is_admin=1, tier="counsel")])
    summary = backfill(sqlite_path=path, pg_session=db_session, enforce_count_band=False)

    assert summary["inserted"] == 0
    assert summary["healed_missing_nowlez"] == 1
    n = db_session.execute(select(UserNowlez).where(UserNowlez.user_id == eid)).scalar_one()
    assert n.legacy_sqlite_id == "leg_merge"
    assert n.name == "Meera"
    # Existing identity + auth untouched.
    u = db_session.execute(select(User).where(User.email == "merge@law.in")).scalar_one()
    assert str(u.id) == str(eid)
    assert u.phone == "+91900111"
    assert u.password_hash == "pw"


def test_links_existing_nowlez_without_legacy_id(tmp_path, db_session):
    existing = User(email="link@law.in", phone="+91900222", password_hash="pw")
    db_session.add(existing)
    db_session.flush()
    db_session.add(UserNowlez(user_id=existing.id, name="Existing"))  # legacy_sqlite_id NULL
    db_session.flush()

    path = _legacy_db(tmp_path, [_row(id="leg_link", email="link@law.in")])
    summary = backfill(sqlite_path=path, pg_session=db_session, enforce_count_band=False)

    assert summary["linked_existing"] == 1
    assert summary["inserted"] == 0
    n = db_session.execute(
        select(UserNowlez).where(UserNowlez.user_id == existing.id)
    ).scalar_one()
    assert n.legacy_sqlite_id == "leg_link"
    assert n.name == "Existing"  # not overwritten


def test_true_conflict_when_linked_to_different_legacy_id(tmp_path, db_session):
    existing = User(email="conf@law.in", phone="+91900333", password_hash="pw")
    db_session.add(existing)
    db_session.flush()
    db_session.add(UserNowlez(user_id=existing.id, name="Other", legacy_sqlite_id="other_legacy"))
    db_session.flush()

    path = _legacy_db(tmp_path, [_row(id="leg_conf", email="conf@law.in")])
    summary = backfill(sqlite_path=path, pg_session=db_session, enforce_count_band=False)

    assert summary["email_conflict"] == 1
    assert summary["inserted"] == 0
    n = db_session.execute(
        select(UserNowlez).where(UserNowlez.user_id == existing.id)
    ).scalar_one()
    assert n.legacy_sqlite_id == "other_legacy"  # untouched


def test_password_hash_is_carried_over(tmp_path, db_session):
    path = _legacy_db(tmp_path, [_row(email="login@law.in", password_hash="bcrypt$keepme")])
    backfill(sqlite_path=path, pg_session=db_session, enforce_count_band=False)
    u = db_session.execute(select(User).where(User.email == "login@law.in")).scalar_one()
    assert u.password_hash == "bcrypt$keepme"


def test_pattern_test_rows_excluded(tmp_path, db_session):
    rows = [
        _row(id="real", email="real@tbalegal.in"),
        _row(id="t1", email="prod-test-1@casepilot-test.invalid"),
        _row(id="t2", email="someone@example.com"),
    ]
    path = _legacy_db(tmp_path, rows)
    summary = backfill(sqlite_path=path, pg_session=db_session, enforce_count_band=False)

    assert summary["inserted"] == 1
    assert db_session.execute(
        select(User).where(User.email == "real@tbalegal.in")
    ).scalar_one_or_none() is not None
    assert db_session.execute(
        select(User).where(User.email == "someone@example.com")
    ).scalar_one_or_none() is None


def test_manual_drop_excluded_though_pattern_clean(tmp_path, db_session):
    drop = sorted(_MANUAL_DROP_EMAILS)[0]
    assert is_test_user(drop)  # authoritative check drops it
    path = _legacy_db(tmp_path, [_row(id="leg_drop", email=drop)])
    summary = backfill(sqlite_path=path, pg_session=db_session, enforce_count_band=False)
    assert summary["inserted"] == 0
    assert db_session.execute(select(User).where(User.email == drop)).scalar_one_or_none() is None


def test_sql_prefilter_is_subset_of_is_test_user():
    """Invariant: every row ``EXCLUDE_TEST_SQL`` removes, ``is_test_user`` also
    removes (the SQL is a coarse pre-cut; is_test_user is authoritative)."""
    sample = [
        "x@casepilot-test.invalid", "y@example.com", "z@test.com",
        "w@testx.com", "test@gmail.com", "foo+e2e-1@gmail.com",
        "prod-test-9@x.invalid", "concurrent12@x.invalid", "nonadmin3@x.invalid",
        "real@rsrlegal.in", "priya@gmail.com",
    ] + list(_MANUAL_DROP_EMAILS)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (email TEXT)")
    conn.executemany("INSERT INTO users(email) VALUES (?)", [(e,) for e in sample])
    sql_excluded = {r[0] for r in conn.execute(f"SELECT email FROM users WHERE {EXCLUDE_TEST_SQL}")}
    conn.close()
    assert sql_excluded  # sanity: the SQL excluded something
    for e in sql_excluded:
        assert is_test_user(e), f"SQL excluded {e!r} but is_test_user kept it"


def test_dry_run_writes_nothing(tmp_path, db_session):
    path = _legacy_db(tmp_path, [_row(id="leg_dry", email="dry@law.in")])
    summary = backfill(sqlite_path=path, pg_session=db_session, dry_run=True, enforce_count_band=False)
    assert summary["dry_run"] is True
    assert summary["inserted"] == 1  # would-insert count
    assert db_session.execute(select(User).where(User.email == "dry@law.in")).scalar_one_or_none() is None


def test_count_band_raises_when_too_few(tmp_path, db_session):
    path = _legacy_db(tmp_path, [_row(email="solo@law.in")])
    # enforce_count_band defaults to apply_test_filter (True); 1 < MIN → raise.
    with pytest.raises(RuntimeError, match="candidate count"):
        backfill(sqlite_path=path, pg_session=db_session)


def test_assert_candidate_count_band():
    assert_candidate_count(EXPECTED_MIN_CANDIDATES)  # lower boundary OK
    with pytest.raises(RuntimeError):
        assert_candidate_count(EXPECTED_MIN_CANDIDATES - 1)
