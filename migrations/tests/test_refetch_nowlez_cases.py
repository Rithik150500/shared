"""Re-fetch script integration test (mocks eCourts, uses in-memory SQLite).

The plan's original sample assumed a Postgres test DB; we adapt it to the
SQLite ``db_session`` fixture (see conftest.py) so the test suite stays
hermetic and doesn't require a running Postgres.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from data_access.daos import case_dao, user_dao
from data_access.models.case import Case as CaseRow
from ecourts_client.models import Case as DataCase, Party

from migrations.refetch_nowlez_cases import main as refetch_main


@pytest.fixture
def seeded_legacy_table(db_session: Session):
    """Seed one row into ``_legacy_nowlez_client_cases`` and return its user_id."""
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876600003")
    db_session.commit()
    db_session.execute(
        text(
            "INSERT INTO _legacy_nowlez_client_cases "
            "(id, user_id, cnr, client_id, refresh_enabled, notes) "
            "VALUES (1, :u, 'MHCC010054732024', NULL, 1, 'old-note')"
        ),
        {"u": str(user.id)},
    )
    db_session.commit()
    return user.id


async def _stub_fetch_case_factory(stub_case: DataCase):
    """Return an async stub that ignores its CNR arg and returns ``stub_case``."""
    async def _stub(cnr: str) -> DataCase:
        return stub_case
    return _stub


@pytest.mark.asyncio
async def test_refetch_inserts_new_row(db_session, seeded_legacy_table):
    stub_case = DataCase(
        cnr="MHCC010054732024",
        title="X v Y",
        court="Test Court",
        stage="Pending",
        next_hearing_date=date(2026, 6, 1),
        judge="J",
        parties=[Party(name="X", role="petitioner")],
    )
    stub = await _stub_fetch_case_factory(stub_case)
    with patch("migrations.refetch_nowlez_cases.fetch_case", new=stub):
        results = await refetch_main(concurrency=2)

    assert results.get("migrated") == 1, results

    row = case_dao.get_by_cnr(
        db_session, user_id=seeded_legacy_table, cnr="MHCC010054732024"
    )
    assert row is not None
    assert row.title == "X v Y"


@pytest.mark.asyncio
async def test_refetch_idempotent(db_session, seeded_legacy_table):
    stub_case = DataCase(
        cnr="MHCC010054732024",
        title="X v Y",
        court="Test Court",
        stage="Pending",
        next_hearing_date=None,
        judge=None,
    )
    stub = await _stub_fetch_case_factory(stub_case)
    with patch("migrations.refetch_nowlez_cases.fetch_case", new=stub):
        first = await refetch_main(concurrency=2)
        second = await refetch_main(concurrency=2)

    assert first.get("migrated") == 1
    assert second.get("skipped") == 1
    assert "migrated" not in second  # second pass touched zero new cases

    count = db_session.scalar(select(func.count()).select_from(CaseRow))
    assert count == 1
