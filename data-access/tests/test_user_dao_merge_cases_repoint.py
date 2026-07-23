"""Repoint-merge must dedupe overlapping `cases` on the (user_id, cnr) and
(user_id, forum, forum_case_ref) unique indexes — survivor wins, the absorbed's
duplicate case (and its CASCADE children) is dropped, non-overlapping cases move.

Regression: before the fix, `cases` was a blind clean-repoint table, so a
survivor+absorbed pair that both track the same CNR raised
`UniqueViolation cases_user_cnr_unique` mid-merge (see the LIVE-USE GATE note in
merge_accounts.py — the SQLite suite never exercised the overlap path).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data_access.daos import user_dao
from data_access.models import Case, User


def _mk_case(session, user_id, *, cnr=None, forum="ecourts_district", forum_case_ref=None):
    ref = forum_case_ref if forum_case_ref is not None else (cnr if cnr is not None else "m-x")
    case = Case(user_id=user_id, cnr=cnr, forum=forum, forum_case_ref=ref)
    session.add(case)
    session.flush()
    return case


def _seed_overlap(session):
    """survivor (older) shares CNR with absorbed; absorbed also owns a unique
    case and a null-cnr forum case that collides on forum_case_ref."""
    older = datetime.now(timezone.utc) - timedelta(days=10)
    survivor = User(email="surv@example.com", created_at=older)
    session.add(survivor)
    session.flush()
    absorbed = User(phone="+919000000001")
    session.add(absorbed)
    session.flush()

    # eCourts CNR overlap -> absorbed's copy must be dropped, survivor's wins.
    _mk_case(session, survivor.id, cnr="MHCT010000012025")
    _mk_case(session, absorbed.id, cnr="MHCT010000012025")
    # Unique eCourts case -> must MOVE to survivor.
    _mk_case(session, absorbed.id, cnr="MHCT010000992025")
    # Null-cnr consumer forum overlap on forum_case_ref -> absorbed's dropped.
    _mk_case(session, survivor.id, forum="consumer", forum_case_ref="DC/99/2025")
    _mk_case(session, absorbed.id, forum="consumer", forum_case_ref="DC/99/2025")
    return survivor, absorbed


def _assert_merged(session, survivor, absorbed):
    surv = {(c.forum, c.forum_case_ref) for c in session.query(Case).filter_by(user_id=survivor.id).all()}
    assert surv == {
        ("ecourts_district", "MHCT010000012025"),
        ("ecourts_district", "MHCT010000992025"),
        ("consumer", "DC/99/2025"),
    }
    assert session.query(Case).filter_by(user_id=absorbed.id).count() == 0


def test_plan_reports_cases_move_and_dropped(db_session):
    survivor, absorbed = _seed_overlap(db_session)
    plan = user_dao.plan_merge_repoint(db_session, survivor_id=survivor.id, absorbed_id=absorbed.id)
    # 1 unique case moves; 2 overlaps (cnr + forum_case_ref) are dropped.
    assert plan["cases"] == 1
    assert plan.get("cases_dropped_dupes") == 2


def test_repoint_merge_dedupes_overlapping_cases_sqlite(db_session):
    survivor, absorbed = _seed_overlap(db_session)
    user_dao.merge_users(db_session, survivor_id=survivor.id, absorbed_id=absorbed.id, repoint=True)
    _assert_merged(db_session, survivor, absorbed)


def test_repoint_merge_dedupes_overlapping_cases_postgres(postgresql_session):
    survivor, absorbed = _seed_overlap(postgresql_session)
    user_dao.merge_users(postgresql_session, survivor_id=survivor.id, absorbed_id=absorbed.id, repoint=True)
    _assert_merged(postgresql_session, survivor, absorbed)
