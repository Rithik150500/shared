"""DAO tests for case_orders + case_orders_nowlez."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

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


# ---------------------------------------------------------------------------
# B.5b — Nowlez hook migration DAO methods
# (mark_order_user_notified / get_unnotified_orders_for_case /
#  get_failed_orders / increment_order_retry_count)
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_user_and_case(db_session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    """Returns (user_id, case_id) for B.5b tests that need user-scoped queries."""
    user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876500099")
    db_session.commit()
    data = DataCase(
        cnr="MHCC010054732024", title="Test", court="X", stage="Pending",
        next_hearing_date=None, judge=None,
    )
    case = case_dao.upsert_case(
        db_session, user_id=user.id, cnr="MHCC010054732024", case_data=data,
    )
    db_session.commit()
    return user.id, case.id


def test_mark_order_user_notified_creates_extension(db_session, fixture_case_id):
    """No extension row yet → mark must create it with user_notified_at set."""
    order_ref = OrderRef(
        order_date=date(2026, 1, 1), order_url="https://x/y.pdf", order_id="ord-n1",
    )
    order_row = order_dao.ensure_munshi_order(
        db_session, case_id=fixture_case_id, order_ref=order_ref,
    )
    db_session.commit()
    # Pre-condition: Munshi order has no nowlez extension.
    assert order_dao.get_nowlez_extension(db_session, order_id=order_row.id) is None

    order_dao.mark_order_user_notified(db_session, order_id=order_row.id)
    db_session.commit()

    ext = order_dao.get_nowlez_extension(db_session, order_id=order_row.id)
    assert ext is not None
    assert ext.user_notified_at is not None


def test_mark_order_user_notified_updates_existing(db_session, fixture_case_id):
    order_ref = OrderRef(
        order_date=date(2026, 1, 1), order_url="https://x/y.pdf", order_id="ord-n2",
    )
    order_row = order_dao.ensure_nowlez_order(
        db_session, case_id=fixture_case_id, order_ref=order_ref,
    )
    db_session.commit()
    ext = order_dao.get_nowlez_extension(db_session, order_id=order_row.id)
    assert ext.user_notified_at is None  # default

    order_dao.mark_order_user_notified(db_session, order_id=order_row.id)
    db_session.commit()
    ext = order_dao.get_nowlez_extension(db_session, order_id=order_row.id)
    assert ext.user_notified_at is not None


def test_get_unnotified_orders_returns_only_unmarked(db_session, fixture_case_id):
    """Three orders: 1 notified (excluded), 1 unnotified-ext, 1 no-ext (Munshi).

    The query must return the two unnotified orders.
    """
    notified_order = order_dao.ensure_nowlez_order(
        db_session, case_id=fixture_case_id,
        order_ref=OrderRef(order_date=date(2026, 1, 3), order_url="u1", order_id="a"),
    )
    unnotified_with_ext = order_dao.ensure_nowlez_order(
        db_session, case_id=fixture_case_id,
        order_ref=OrderRef(order_date=date(2026, 1, 2), order_url="u2", order_id="b"),
    )
    unnotified_no_ext = order_dao.ensure_munshi_order(
        db_session, case_id=fixture_case_id,
        order_ref=OrderRef(order_date=date(2026, 1, 1), order_url="u3", order_id="c"),
    )
    order_dao.mark_order_user_notified(db_session, order_id=notified_order.id)
    db_session.commit()

    unnotified = order_dao.get_unnotified_orders_for_case(
        db_session, case_id=fixture_case_id,
    )
    ids = {o.id for o in unnotified}
    assert unnotified_with_ext.id in ids
    assert unnotified_no_ext.id in ids
    assert notified_order.id not in ids


def test_get_failed_orders_filters_by_predicate(db_session, fixture_case_id):
    """Build a mix of orders and assert exactly the eligible ones come back.

    Eligible = preprocessed=False AND permanently_failed=False AND
               retry_count < max_retries AND cooldown passed.
    """
    def _make_order(suffix: str, *, preprocessed=False, permanently_failed=False,
                    retry_count=0, last_retry_at=None) -> uuid.UUID:
        o = order_dao.ensure_nowlez_order(
            db_session, case_id=fixture_case_id,
            order_ref=OrderRef(
                order_date=date(2026, 1, 1),
                order_url=f"https://x/{suffix}.pdf",
                order_id=f"ord-{suffix}",
            ),
        )
        order_dao.upsert_nowlez_extension(
            db_session, order_id=o.id,
            preprocessed=preprocessed,
            permanently_failed=permanently_failed,
            retry_count=retry_count,
            last_retry_at=last_retry_at,
        )
        return o.id

    eligible = _make_order("eligible")  # never retried → cooldown passes
    eligible_old = _make_order(
        "eligible-old", retry_count=1,
        last_retry_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    not_eligible_preprocessed = _make_order("preprocessed", preprocessed=True)
    not_eligible_failed = _make_order("failed", permanently_failed=True)
    not_eligible_max_retries = _make_order("maxed", retry_count=3)
    not_eligible_recent_retry = _make_order(
        "recent", retry_count=1,
        last_retry_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db_session.commit()

    rows = order_dao.get_failed_orders(db_session, max_retries=3)
    ids = {co.id for co, _ext in rows}
    assert eligible in ids
    assert eligible_old in ids
    assert not_eligible_preprocessed not in ids
    assert not_eligible_failed not in ids
    assert not_eligible_max_retries not in ids
    assert not_eligible_recent_retry not in ids


def test_get_failed_orders_scopes_to_user(
    db_session, fixture_user_and_case,
):
    """When user_id is provided, only orders owned by that user are returned."""
    user_id, case_id = fixture_user_and_case
    # Owned-by-user order.
    mine = order_dao.ensure_nowlez_order(
        db_session, case_id=case_id,
        order_ref=OrderRef(order_date=date(2026, 1, 1), order_url="u1", order_id="mine"),
    )
    # Orphan order belonging to a different user / different case.
    other_user, _ = user_dao.get_or_create_by_phone(db_session, phone="+919876500100")
    other_data = DataCase(
        cnr="MHCC019999992024", title="Other", court="Y", stage="Pending",
        next_hearing_date=None, judge=None,
    )
    other_case = case_dao.upsert_case(
        db_session, user_id=other_user.id, cnr="MHCC019999992024", case_data=other_data,
    )
    theirs = order_dao.ensure_nowlez_order(
        db_session, case_id=other_case.id,
        order_ref=OrderRef(order_date=date(2026, 1, 1), order_url="u2", order_id="theirs"),
    )
    db_session.commit()

    scoped = order_dao.get_failed_orders(db_session, user_id=user_id, max_retries=3)
    scoped_ids = {co.id for co, _ in scoped}
    assert mine.id in scoped_ids
    assert theirs.id not in scoped_ids

    unscoped = order_dao.get_failed_orders(db_session, max_retries=3)
    unscoped_ids = {co.id for co, _ in unscoped}
    assert mine.id in unscoped_ids
    assert theirs.id in unscoped_ids


def test_increment_order_retry_count_returns_new_count(db_session, fixture_case_id):
    order_ref = OrderRef(
        order_date=date(2026, 1, 1), order_url="u", order_id="ord-inc",
    )
    order = order_dao.ensure_nowlez_order(
        db_session, case_id=fixture_case_id, order_ref=order_ref,
    )
    db_session.commit()

    n1 = order_dao.increment_order_retry_count(
        db_session, order_id=order.id, max_retries=5,
    )
    db_session.commit()
    n2 = order_dao.increment_order_retry_count(
        db_session, order_id=order.id, max_retries=5,
    )
    db_session.commit()
    assert n1 == 1
    assert n2 == 2

    ext = order_dao.get_nowlez_extension(db_session, order_id=order.id)
    assert ext.retry_count == 2
    assert ext.last_retry_at is not None
    assert ext.permanently_failed is False  # 2 < 5


def test_increment_order_retry_count_marks_permanently_failed_at_max(
    db_session, fixture_case_id,
):
    order_ref = OrderRef(
        order_date=date(2026, 1, 1), order_url="u", order_id="ord-max",
    )
    order = order_dao.ensure_nowlez_order(
        db_session, case_id=fixture_case_id, order_ref=order_ref,
    )
    # Seed retry_count to one below max so the next increment hits the cap.
    order_dao.upsert_nowlez_extension(db_session, order_id=order.id, retry_count=2)
    db_session.commit()

    n = order_dao.increment_order_retry_count(
        db_session, order_id=order.id, max_retries=3,
    )
    db_session.commit()
    assert n == 3
    ext = order_dao.get_nowlez_extension(db_session, order_id=order.id)
    assert ext.permanently_failed is True


def test_increment_order_retry_count_raises_when_missing_extension(
    db_session, fixture_case_id,
):
    """Munshi-only order (no nowlez extension) → LookupError, not auto-create.

    The retry pipeline only runs against nowlez orders, so getting here
    means a programming error in the caller — fail loudly instead of
    quietly upserting a fresh extension with retry_count=1.
    """
    order_ref = OrderRef(
        order_date=date(2026, 1, 1), order_url="u", order_id="ord-no-ext",
    )
    order = order_dao.ensure_munshi_order(
        db_session, case_id=fixture_case_id, order_ref=order_ref,
    )
    db_session.commit()
    with pytest.raises(LookupError):
        order_dao.increment_order_retry_count(db_session, order_id=order.id)


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
