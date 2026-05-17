"""DAO tests for case_orders + case_orders_nowlez."""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.orm import Session

from data_access.daos import case_dao, order_dao, user_dao
from ecourts_client.models import Case as DataCase, OrderRef


@pytest.fixture
def fixture_case_id(db_session: Session) -> uuid.UUID:
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876500002")
    db_session.commit()
    data = DataCase(
        cnr="MHCC010054732024", title="Test", court="X", stage="Pending",
        next_hearing_date=None, judge=None,
    )
    c = case_dao.upsert_case(db_session, user_id=user.id, cnr="MHCC010054732024", case_data=data)
    db_session.commit()
    return c.id


def test_ensure_munshi_order_only_creates_case_orders_row(db_session, fixture_case_id):
    order_ref = OrderRef(order_date=date(2026, 1, 1), order_url="https://x/y.pdf", order_id="ord-1")
    order_row = order_dao.ensure_munshi_order(db_session, case_id=fixture_case_id, order_ref=order_ref)
    db_session.commit()
    assert order_row.order_id == "ord-1"
    assert order_dao.get_nowlez_extension(db_session, order_id=order_row.id) is None


def test_ensure_nowlez_order_creates_both_rows(db_session, fixture_case_id):
    order_ref = OrderRef(order_date=date(2026, 1, 1), order_url="https://x/y.pdf", order_id="ord-2")
    order_row = order_dao.ensure_nowlez_order(db_session, case_id=fixture_case_id, order_ref=order_ref)
    db_session.commit()
    ext = order_dao.get_nowlez_extension(db_session, order_id=order_row.id)
    assert ext is not None
    assert ext.preprocessed is False


def test_upsert_nowlez_extension_preserves_pdf_state(db_session, fixture_case_id):
    order_ref = OrderRef(order_date=date(2026, 1, 1), order_url="https://x/y.pdf", order_id="ord-3")
    order_row = order_dao.ensure_nowlez_order(db_session, case_id=fixture_case_id, order_ref=order_ref)
    db_session.commit()
    order_dao.upsert_nowlez_extension(
        db_session, order_id=order_row.id, file_path="orders/abc.pdf",
        file_storage="r2", page_count=5, preprocessed=True,
    )
    db_session.commit()
    ext = order_dao.get_nowlez_extension(db_session, order_id=order_row.id)
    assert ext.file_path == "orders/abc.pdf"
    assert ext.preprocessed is True


def test_get_orders_for_case_ordered_by_date_desc(db_session, fixture_case_id):
    order_dao.ensure_nowlez_order(
        db_session, case_id=fixture_case_id,
        order_ref=OrderRef(order_date=date(2025, 1, 1), order_url="u1", order_id="a"),
    )
    order_dao.ensure_nowlez_order(
        db_session, case_id=fixture_case_id,
        order_ref=OrderRef(order_date=date(2026, 1, 1), order_url="u2", order_id="b"),
    )
    db_session.commit()
    orders = order_dao.get_orders_for_case(db_session, case_id=fixture_case_id)
    assert orders[0].order_id == "b"
    assert orders[1].order_id == "a"


def test_get_legacy_orders_by_case_uses_canonical_column_name(db_session):
    """Pins the schema contract between sub-project D and the re-fetch DAO.

    The legacy schema column is ``client_case_id`` (historical artefact;
    see RUNBOOK_refetch_nowlez_cases.md §0.1). If sub-project D's cutover
    SQL ever produces a different name (e.g. ``legacy_case_id``), this
    test breaks loudly instead of silently returning empty results in
    production.
    """
    from sqlalchemy import text
    db_session.execute(text(
        "CREATE TABLE IF NOT EXISTS _legacy_nowlez_case_orders ("
        "id INTEGER PRIMARY KEY, "
        "client_case_id INTEGER, "
        "order_id TEXT, "
        "file_path TEXT"
        ")"
    ))
    db_session.execute(text(
        "INSERT INTO _legacy_nowlez_case_orders (id, client_case_id, order_id, file_path) "
        "VALUES (1, 42, 'ord-x', 'orders/x.pdf')"
    ))
    db_session.commit()

    rows = order_dao.get_legacy_orders_by_case(db_session, legacy_case_id=42)
    assert len(rows) == 1
    assert rows[0]["order_id"] == "ord-x"
    assert rows[0]["file_path"] == "orders/x.pdf"
