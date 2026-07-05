"""Tribunal family (T1): generic `tribunal` forum + tribunal_kind sub-type.

Runs on the SQLite ``db_session`` fixture (the model's create_all expresses the
final tribunal shape incl. the two partial unique indexes with sqlite_where).
Verifies the manual create path carries tribunal_kind, the set-IFF-tribunal
consistency guard, refresh gating for source='tribunal_auto', and — the
load-bearing case — that two tribunal KINDS sharing a case ref coexist (the
uniqueness split) while a same-kind duplicate collides.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data_access.daos import case_dao, user_dao


@pytest.fixture
def uid(db_session: Session) -> uuid.UUID:
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876500077")
    user_dao.ensure_nowlez_extension(db_session, user.id, name="Tribunal User")
    db_session.commit()
    return user.id


def test_manual_tribunal_case_carries_kind(db_session, uid):
    row = case_dao.create_manual_case(
        db_session, user_id=uid, forum="tribunal", tribunal_kind="nclt",
        forum_case_ref="CP(IB)/123/2025", title="ACME v. Beta",
    )
    db_session.commit()
    assert row.forum == "tribunal"
    assert row.tribunal_kind == "nclt"
    assert row.cnr is None
    assert row.source == "manual"
    assert row.refresh_enabled is False


def test_tribunal_requires_kind(db_session, uid):
    with pytest.raises(ValueError, match="requires a tribunal_kind"):
        case_dao.create_manual_case(
            db_session, user_id=uid, forum="tribunal", forum_case_ref="X/1/2025",
        )


def test_non_tribunal_forum_rejects_kind(db_session, uid):
    with pytest.raises(ValueError, match="must not carry a tribunal_kind"):
        case_dao.create_manual_case(
            db_session, user_id=uid, forum="consumer", tribunal_kind="nclt",
            forum_case_ref="CC/1/2025",
        )


def test_tribunal_auto_is_polled_manual_is_not(db_session, uid):
    case_dao.create_manual_case(
        db_session, user_id=uid, forum="tribunal", tribunal_kind="nclat",
        forum_case_ref="AT/9/2025", source="tribunal_auto", refresh_enabled=True,
    )
    case_dao.create_manual_case(
        db_session, user_id=uid, forum="tribunal", tribunal_kind="sat",
        forum_case_ref="AP/2/2025",  # manual (default) — must be excluded
    )
    db_session.commit()
    due_refs = {c.forum_case_ref for c in case_dao.get_due_for_refresh(db_session)}
    assert "AT/9/2025" in due_refs
    assert "AP/2/2025" not in due_refs


def test_two_kinds_same_ref_coexist_but_same_kind_collides(db_session, uid):
    # The uniqueness split: (user, tribunal, nclt, ref) and (user, tribunal, cat,
    # ref) share a ref but differ by kind → both persist.
    case_dao.create_manual_case(
        db_session, user_id=uid, forum="tribunal", tribunal_kind="nclt",
        forum_case_ref="77/2025",
    )
    case_dao.create_manual_case(
        db_session, user_id=uid, forum="tribunal", tribunal_kind="cat",
        forum_case_ref="77/2025",
    )
    db_session.commit()
    rows = case_dao.list_by_user(db_session, user_id=uid)
    kinds = {r.tribunal_kind for r in rows if r.forum_case_ref == "77/2025"}
    assert kinds == {"nclt", "cat"}

    # A same-(user, tribunal, kind, ref) duplicate must violate the tribunal index.
    # create_manual_case upserts by (user, forum, ref) via get_by_ref, so force a
    # raw duplicate row to exercise the unique index directly.
    from data_access.models import Case
    db_session.add(Case(
        user_id=uid, cnr=None, portal=None, forum="tribunal", tribunal_kind="nclt",
        forum_case_ref="77/2025", source="manual", refresh_enabled=False,
        raw_response={},
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
