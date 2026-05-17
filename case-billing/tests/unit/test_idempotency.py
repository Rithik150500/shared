"""Unit tests for ``case_billing.shared.idempotency`` (Task 8.1.2).

Exercises the two-function surface end-to-end against in-memory SQLite:

* :func:`is_event_already_processed` returns False on a fresh DB, True
  once a row exists.
* :func:`record_event_processed` inserts and returns True.
* A second insert with the same ``razorpay_event_id`` raises
  ``IntegrityError`` internally; we catch it and return False so the
  caller can treat it as "already processed" without rolling back the
  outer transaction.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.shared.idempotency import (
    is_event_already_processed,
    record_event_processed,
)


@pytest.fixture()
def session() -> Session:
    from data_access.base import Base

    # Side-effect: importing the modules registers their models on
    # ``Base.metadata`` so create_all builds every table.
    import data_access.models.auth  # noqa: F401
    import data_access.models.billing  # noqa: F401
    import data_access.models.case  # noqa: F401
    import data_access.models.user  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_is_event_already_processed_false_on_empty_db(session: Session) -> None:
    assert _run(is_event_already_processed("evt_new", session)) is False


def test_is_event_already_processed_false_when_event_id_empty(
    session: Session,
) -> None:
    # Empty event id is a degenerate case — return False without a
    # SELECT round-trip.
    assert _run(is_event_already_processed("", session)) is False


def test_record_event_processed_inserts_row(session: Session) -> None:
    from data_access.models.billing import PaymentEvent

    inserted = _run(
        record_event_processed(
            razorpay_event_id="evt_abc123",
            event_type="subscription.activated",
            product="nowlez",
            payload={"event": "subscription.activated", "id": "evt_abc123"},
            session=session,
        )
    )
    assert inserted is True
    session.flush()

    rows = session.execute(select(PaymentEvent)).scalars().all()
    assert len(rows) == 1
    assert rows[0].razorpay_event_id == "evt_abc123"
    assert rows[0].event_type == "subscription.activated"
    assert rows[0].product == "nowlez"
    assert rows[0].processed_at is not None


def test_record_event_processed_persists_processing_result(
    session: Session,
) -> None:
    from data_access.models.billing import PaymentEvent

    _run(
        record_event_processed(
            razorpay_event_id="evt_with_result",
            event_type="invoice.paid",
            product="munshi",
            payload={"id": "evt_with_result"},
            session=session,
            processing_result={"status": "processed", "invoice_id": "inv_1"},
        )
    )
    session.flush()

    row = session.execute(
        select(PaymentEvent).where(
            PaymentEvent.razorpay_event_id == "evt_with_result"
        )
    ).scalar_one()
    assert row.payload["__processing_result__"]["status"] == "processed"


def test_record_event_processed_persists_user_id(session: Session) -> None:
    from data_access.models.billing import PaymentEvent

    user_id = uuid.uuid4()
    _run(
        record_event_processed(
            razorpay_event_id="evt_with_user",
            event_type="invoice.paid",
            product="munshi",
            payload={"id": "evt_with_user"},
            session=session,
            user_id=user_id,
        )
    )
    session.flush()

    row = session.execute(
        select(PaymentEvent).where(
            PaymentEvent.razorpay_event_id == "evt_with_user"
        )
    ).scalar_one()
    assert row.payload["__user_id__"] == str(user_id)


def test_is_event_already_processed_true_after_insert(session: Session) -> None:
    _run(
        record_event_processed(
            razorpay_event_id="evt_seen",
            event_type="subscription.charged",
            product="nowlez",
            payload={"id": "evt_seen"},
            session=session,
        )
    )
    session.flush()
    assert _run(is_event_already_processed("evt_seen", session)) is True


def test_record_event_processed_returns_false_on_duplicate(
    session: Session,
) -> None:
    """Second INSERT with the same event id hits the UNIQUE constraint;
    we catch the IntegrityError and return False without poisoning the
    outer transaction."""
    first = _run(
        record_event_processed(
            razorpay_event_id="evt_dup",
            event_type="subscription.activated",
            product="nowlez",
            payload={"id": "evt_dup"},
            session=session,
        )
    )
    session.flush()
    assert first is True

    # Second insert with same id.
    second = _run(
        record_event_processed(
            razorpay_event_id="evt_dup",
            event_type="subscription.activated",
            product="nowlez",
            payload={"id": "evt_dup", "retry": True},
            session=session,
        )
    )
    assert second is False

    # And only one row persists.
    from data_access.models.billing import PaymentEvent
    rows = session.execute(
        select(PaymentEvent).where(
            PaymentEvent.razorpay_event_id == "evt_dup"
        )
    ).scalars().all()
    assert len(rows) == 1


def test_record_event_processed_empty_id_returns_false(session: Session) -> None:
    """Empty event id is a programming error elsewhere — fail closed
    rather than persisting a row with NULL/blank id."""
    inserted = _run(
        record_event_processed(
            razorpay_event_id="",
            event_type="subscription.activated",
            product="nowlez",
            payload={},
            session=session,
        )
    )
    assert inserted is False
