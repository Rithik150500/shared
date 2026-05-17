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
