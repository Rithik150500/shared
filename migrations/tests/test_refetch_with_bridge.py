"""Verify refetch_nowlez_cases.py cleans up uuid5 placeholder rows that
PR 5.3's best-effort dual-write may have written to `cases`.

Background: during the PR 5.3 bake period, the Nowlez backend
(``backend/preprocessing.py``) best-effort-writes each new case to
Postgres alongside SQLite. When the phone bridge fails to resolve a
real ``users.id`` it falls back to ``uuid.uuid5(_PG_USER_FALLBACK_NS,
client_id)`` (see ``backend/helpers.py::_resolve_pg_user_id`` at
commit ``fef6e60`` on branch ``feat/subproject-a-pr6-cutover``). Those
placeholder rows must NOT survive the PR 6 cutover: after the refetch,
each ``(real_user_id, cnr)`` row should be the only entry per case.

The cutover refetch is the right place to clean those up because (a)
it touches every legacy case exactly once and (b) it has the real
``users.id`` already (D's cutover put it in
``_legacy_nowlez_client_cases.user_id``). The PR 6 A.4 change adds a
delete-then-upsert step keyed on the placeholder UUID.

These tests use the in-memory SQLite ``db_session`` fixture from
``conftest.py`` (no Postgres needed). The uuid5 namespace constant MUST
match the Nowlez backend; drift would mean placeholders accumulated
during the bake won't get cleaned up here.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date
from unittest.mock import patch

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from data_access.daos import case_dao, user_dao
from data_access.models.case import Case as CaseRow
from ecourts_client.models import Case as DataCase, Party

from migrations.refetch_nowlez_cases import (
    _PG_USER_FALLBACK_NS,
    main as refetch_main,
)


# Sanity-check: the namespace UUID must match the Nowlez backend
# (backend/helpers.py at commit fef6e60). Hard-coded here as a regression
# guard so a typo on either side surfaces as a failing test, not as silently
# orphaned placeholder rows.
_EXPECTED_NS = uuid.UUID("5fa9c8c8-3a17-4b9e-9c4e-0c2cf5e1d9aa")


def test_placeholder_namespace_matches_backend() -> None:
    assert _PG_USER_FALLBACK_NS == _EXPECTED_NS, (
        "refetch_nowlez_cases._PG_USER_FALLBACK_NS drifted from the Nowlez "
        "backend constant in backend/helpers.py (commit fef6e60). They must "
        "be byte-identical so placeholders written by PR 5.3 get matched "
        "and cleaned up here."
    )


async def _stub_fetch_case_factory(stub_case: DataCase):
    async def _stub(cnr: str) -> DataCase:
        return stub_case
    return _stub


@pytest.fixture
def seeded_legacy_with_placeholder(db_session: Session):
    """Pre-seed: PR 5.3 wrote a uuid5-placeholder row to ``cases``; D's
    cutover put the real user_id in ``_legacy_nowlez_client_cases``.

    Yields (real_user_id, placeholder_uid, cnr, client_id) so the test can
    assert the placeholder row has been removed after refetch and the
    canonical (real_user_id, cnr) row is in place.
    """
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876600099")
    db_session.commit()

    client_id = "client-bake-001"
    cnr = "MHCC010054732024"
    placeholder_uid = uuid.uuid5(_PG_USER_FALLBACK_NS, client_id)

    # Pre-existing placeholder row in `cases` (PR 5.3 best-effort bake).
    placeholder_case = DataCase(
        cnr=cnr,
        title="STALE PLACEHOLDER",
        court="Test Court",
        stage="Pending",
        next_hearing_date=None,
        judge=None,
    )
    case_dao.upsert_case(
        db_session,
        user_id=placeholder_uid,
        cnr=cnr,
        case_data=placeholder_case,
        client_id=client_id,
    )

    # Legacy table row (D's cutover) — user_id is the REAL Postgres User.id.
    db_session.execute(
        text(
            "INSERT INTO _legacy_nowlez_client_cases "
            "(id, user_id, cnr, client_id, refresh_enabled, notes) "
            "VALUES (1, :u, :cnr, :cid, 1, NULL)"
        ),
        {"u": str(user.id), "cnr": cnr, "cid": client_id},
    )
    db_session.commit()

    return user.id, placeholder_uid, cnr, client_id


@pytest.mark.asyncio
async def test_refetch_overwrites_uuid5_placeholder_with_real_user_id(
    db_session, seeded_legacy_with_placeholder
):
    """After refetch, the placeholder ``(uuid5, cnr)`` row is gone and the
    canonical ``(real_uid, cnr)`` row has the fresh fetch payload."""
    real_uid, placeholder_uid, cnr, _ = seeded_legacy_with_placeholder

    # Sanity-check the test setup: placeholder row must exist pre-refetch.
    pre = case_dao.get_by_cnr(db_session, user_id=placeholder_uid, cnr=cnr)
    assert pre is not None, "test setup broken: placeholder row was not seeded"
    assert pre.title == "STALE PLACEHOLDER"

    fresh = DataCase(
        cnr=cnr,
        title="FRESH X v Y",
        court="Test Court",
        stage="Pending",
        next_hearing_date=date(2026, 6, 1),
        judge="J",
        parties=[Party(name="X", role="petitioner")],
    )
    stub = await _stub_fetch_case_factory(fresh)
    with patch("migrations.refetch_nowlez_cases.fetch_case", new=stub):
        results = await refetch_main(concurrency=1)

    assert results.get("migrated") == 1, results

    # Canonical row is in place under the real user_id with fresh data.
    canonical = case_dao.get_by_cnr(db_session, user_id=real_uid, cnr=cnr)
    assert canonical is not None, "canonical (real_uid, cnr) row missing"
    assert canonical.title == "FRESH X v Y"

    # Placeholder row is gone.
    leftover = case_dao.get_by_cnr(db_session, user_id=placeholder_uid, cnr=cnr)
    assert leftover is None, (
        f"placeholder row at user_id={placeholder_uid} survived refetch; "
        "PR 6 A.4 cleanup did not run"
    )

    # Exactly one row in `cases` for this CNR.
    total = db_session.scalar(
        select(func.count()).select_from(CaseRow).where(CaseRow.cnr == cnr)
    )
    assert total == 1, f"expected 1 canonical row, found {total}"


@pytest.mark.asyncio
async def test_refetch_logs_placeholder_cleanup(
    db_session, seeded_legacy_with_placeholder, caplog
):
    """Cleanup of a placeholder row emits an INFO log naming the client_id
    so operators can audit how many placeholders accrued during the bake."""
    _, _, cnr, client_id = seeded_legacy_with_placeholder

    fresh = DataCase(
        cnr=cnr,
        title="Fresh",
        court="Test Court",
        stage="Pending",
        next_hearing_date=None,
        judge=None,
    )
    stub = await _stub_fetch_case_factory(fresh)
    caplog.set_level(logging.INFO, logger="migrations.refetch_nowlez_cases")
    with patch("migrations.refetch_nowlez_cases.fetch_case", new=stub):
        await refetch_main(concurrency=1)

    matches = [
        rec for rec in caplog.records
        if "placeholder" in rec.message.lower() and client_id in rec.message
    ]
    assert matches, (
        f"expected an INFO log line mentioning placeholder cleanup for "
        f"client_id={client_id!r}; got:\n"
        + "\n".join(f"  {r.levelname} {r.message}" for r in caplog.records)
    )


@pytest.mark.asyncio
async def test_refetch_no_placeholder_is_noop(db_session: Session):
    """When PR 5.3 never wrote a placeholder for a given client (because the
    bridge resolved at write-time), the refetch behaves exactly as before:
    one row gets created under the real user_id and nothing else is touched.

    This is the no-bake-collision happy path and guards against the cleanup
    step accidentally deleting the real row itself.
    """
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876600100")
    db_session.commit()

    client_id = "client-clean-001"
    cnr = "MHCC010054732025"

    db_session.execute(
        text(
            "INSERT INTO _legacy_nowlez_client_cases "
            "(id, user_id, cnr, client_id, refresh_enabled, notes) "
            "VALUES (1, :u, :cnr, :cid, 1, NULL)"
        ),
        {"u": str(user.id), "cnr": cnr, "cid": client_id},
    )
    db_session.commit()

    fresh = DataCase(
        cnr=cnr,
        title="Brand New",
        court="Test Court",
        stage="Pending",
        next_hearing_date=None,
        judge=None,
    )
    stub = await _stub_fetch_case_factory(fresh)
    with patch("migrations.refetch_nowlez_cases.fetch_case", new=stub):
        results = await refetch_main(concurrency=1)

    assert results.get("migrated") == 1, results
    canonical = case_dao.get_by_cnr(db_session, user_id=user.id, cnr=cnr)
    assert canonical is not None and canonical.title == "Brand New"

    total = db_session.scalar(
        select(func.count()).select_from(CaseRow).where(CaseRow.cnr == cnr)
    )
    assert total == 1


@pytest.mark.asyncio
async def test_refetch_skips_cleanup_when_client_id_is_null(db_session: Session):
    """Some legacy rows have ``client_id IS NULL`` (Nowlez supported
    user-direct CNR adds without a Mobile App ``Client`` parent). Those
    rows could not have produced a PR 5.3 placeholder (no client_id → no
    uuid5), so the cleanup step must short-circuit. Test that the script
    doesn't crash on NULL client_id and the canonical row is still
    written correctly.
    """
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876600101")
    db_session.commit()

    cnr = "MHCC010054732026"
    db_session.execute(
        text(
            "INSERT INTO _legacy_nowlez_client_cases "
            "(id, user_id, cnr, client_id, refresh_enabled, notes) "
            "VALUES (1, :u, :cnr, NULL, 1, NULL)"
        ),
        {"u": str(user.id), "cnr": cnr},
    )
    db_session.commit()

    fresh = DataCase(
        cnr=cnr,
        title="No Client",
        court="Test Court",
        stage="Pending",
        next_hearing_date=None,
        judge=None,
    )
    stub = await _stub_fetch_case_factory(fresh)
    with patch("migrations.refetch_nowlez_cases.fetch_case", new=stub):
        results = await refetch_main(concurrency=1)

    assert results.get("migrated") == 1, results
    canonical = case_dao.get_by_cnr(db_session, user_id=user.id, cnr=cnr)
    assert canonical is not None
