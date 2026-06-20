"""Guard + reconcile for phone-format drift in the users table.

find_phone_format_collisions() is the monitor: it flags any set of user rows
that normalize to the same E.164 number but are stored in different formats
(the identity-split signature). reconcile_phone_formats() is the one-time
canonicalizer: it safely renames lone non-canonical rows and REFUSES to
auto-merge true collisions (those are reported for human review).
"""
from data_access.models import User
from data_access.phone_reconcile import (
    find_phone_format_collisions,
    reconcile_phone_formats,
)


def _add_raw_user(session, phone):
    """Insert a User with an exact phone string, bypassing DAO normalization,
    to simulate legacy rows written before the normalizer existed."""
    u = User(phone=phone, locale="en")
    session.add(u)
    session.flush()
    return u


# --- guard / monitor ---

def test_collision_detector_flags_format_duplicates(db_session):
    a = _add_raw_user(db_session, "9953652710")
    b = _add_raw_user(db_session, "+919953652710")
    collisions = find_phone_format_collisions(db_session)
    assert "+919953652710" in collisions
    # Compare as str: SQLite returns str PKs for re-SELECTed rows while the
    # directly-constructed a/b carry uuid.UUID PKs (Postgres has no such split).
    assert {str(u.id) for u in collisions["+919953652710"]} == {str(a.id), str(b.id)}


def test_collision_detector_silent_when_all_canonical(db_session):
    _add_raw_user(db_session, "+919953652710")
    _add_raw_user(db_session, "+919518200090")
    assert find_phone_format_collisions(db_session) == {}


# --- reconcile ---

def test_reconcile_canonicalizes_lone_non_canonical_row(db_session):
    u = _add_raw_user(db_session, "9518200090")  # Kunal-type: no twin
    report = reconcile_phone_formats(db_session, dry_run=False)
    db_session.refresh(u)
    assert u.phone == "+919518200090"
    assert report["renamed"] == [{"id": str(u.id), "old": "9518200090", "new": "+919518200090"}]
    assert report["collisions"] == []


def test_reconcile_dry_run_changes_nothing(db_session):
    u = _add_raw_user(db_session, "9518200090")
    report = reconcile_phone_formats(db_session, dry_run=True)
    db_session.refresh(u)
    assert u.phone == "9518200090"  # untouched
    assert len(report["renamed"]) == 1  # but planned


def test_reconcile_refuses_to_merge_a_collision(db_session):
    a = _add_raw_user(db_session, "9953652710")
    b = _add_raw_user(db_session, "+919953652710")
    report = reconcile_phone_formats(db_session, dry_run=False)
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.phone == "9953652710"  # NOT auto-changed
    assert b.phone == "+919953652710"
    assert report["renamed"] == []
    assert len(report["collisions"]) == 1
    assert report["collisions"][0]["canonical"] == "+919953652710"
