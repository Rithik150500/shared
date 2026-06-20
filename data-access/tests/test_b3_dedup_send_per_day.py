"""B-3 dedup: ``whatsapp_delivery_log`` partial unique index tests.

Covers the SQL-level guarantee that the audit's cross-pod duplicate-send
bug is now impossible at the DB layer, regardless of which producer
inserts the row. The tests use the SQLite ``db_session`` fixture (modern
SQLite supports partial unique indexes since 3.8) so they run as part of
the normal data-access unit-test pass without a Postgres dependency.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from data_access.daos import user_dao
from data_access.models import User, WhatsAppDeliveryLog


def _make_user(s, phone: str = "+919876543210") -> User:
    user, _ = user_dao.get_or_create_by_phone(s, phone=phone)
    s.commit()
    return user


def test_send_date_ist_column_exists():
    """Sanity check: the new column is declared on the model."""
    cols = {c.name for c in WhatsAppDeliveryLog.__table__.columns}
    assert "send_date_ist" in cols
    col = WhatsAppDeliveryLog.__table__.c.send_date_ist
    assert col.nullable is True, (
        "send_date_ist must be nullable so transactional sends are exempt"
    )


def test_partial_unique_index_declared():
    """The partial unique index is declared on the table."""
    table = WhatsAppDeliveryLog.__table__
    target = "whatsapp_delivery_log_user_template_day_unique"
    matches = [i for i in table.indexes if i.name == target]
    assert matches, f"expected index {target!r}; got {[i.name for i in table.indexes]}"
    idx = matches[0]
    assert idx.unique is True
    col_names = [c.name for c in idx.columns]
    assert col_names == ["user_id", "template_name", "send_date_ist"], (
        f"expected (user_id, template_name, send_date_ist); got {col_names}"
    )


def test_same_user_same_template_same_day_raises_integrity_error(db_session):
    """The unique index makes two rows with the same dedup key impossible."""
    u = _make_user(db_session)
    row1 = WhatsAppDeliveryLog(
        user_id=u.id,
        template_name="nowlez_tomorrow_hearings_v1",
        brand="nowlez",
        send_date_ist=date(2026, 5, 20),
    )
    db_session.add(row1)
    db_session.commit()

    row2 = WhatsAppDeliveryLog(
        user_id=u.id,
        template_name="nowlez_tomorrow_hearings_v1",
        brand="nowlez",
        send_date_ist=date(2026, 5, 20),
    )
    db_session.add(row2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def _insert_row(s, **kw) -> WhatsAppDeliveryLog:
    """Insert + commit one row at a time.

    SQLAlchemy 2.x's bulk INSERT sentinel-matching path collides with the
    sqlite3 UUID adapter when two pending rows are added in the same flush
    (the adapter stringifies UUIDs for binding but the sentinel matcher
    looks them up by the original UUID object). Per-row commits sidestep
    the bulk path. This is a test-only constraint; production runs against
    Postgres where the bulk INSERT path is fine.
    """
    row = WhatsAppDeliveryLog(**kw)
    s.add(row)
    s.commit()
    return row


def test_different_day_same_user_template_is_allowed(db_session):
    """The cron must be able to send the same template on two different days."""
    u = _make_user(db_session)
    _insert_row(
        db_session,
        user_id=u.id,
        template_name="nowlez_tomorrow_hearings_v1",
        brand="nowlez",
        send_date_ist=date(2026, 5, 20),
    )
    _insert_row(
        db_session,
        user_id=u.id,
        template_name="nowlez_tomorrow_hearings_v1",
        brand="nowlez",
        send_date_ist=date(2026, 5, 21),
    )
    rows = db_session.query(WhatsAppDeliveryLog).filter_by(user_id=u.id).all()
    assert len(rows) == 2


def test_null_send_date_rows_are_exempt_from_unique(db_session):
    """Transactional sends (NULL send_date_ist) must coexist multiple times.

    Signup welcome, OTP, order-uploaded etc. set ``send_date_ist=NULL`` so
    they bypass the dedup guarantee — a user might legitimately upload two
    orders in five minutes and need both notifications.
    """
    u = _make_user(db_session)
    _insert_row(
        db_session,
        user_id=u.id,
        template_name="nowlez_signup_welcome_v2",
        brand="nowlez",
        send_date_ist=None,
    )
    _insert_row(
        db_session,
        user_id=u.id,
        template_name="nowlez_signup_welcome_v2",
        brand="nowlez",
        send_date_ist=None,
    )
    rows = db_session.query(WhatsAppDeliveryLog).filter_by(
        user_id=u.id, template_name="nowlez_signup_welcome_v2",
    ).all()
    assert len(rows) == 2, "NULL send_date_ist rows must NOT collide"


def test_different_template_same_day_is_allowed(db_session):
    """User can receive ``nowlez_tomorrow_hearings_v1`` AND
    ``nowlez_weekly_summary_v1`` on the same day."""
    u = _make_user(db_session)
    today = date(2026, 5, 20)
    _insert_row(
        db_session,
        user_id=u.id,
        template_name="nowlez_tomorrow_hearings_v1",
        brand="nowlez",
        send_date_ist=today,
    )
    _insert_row(
        db_session,
        user_id=u.id,
        template_name="nowlez_weekly_summary_v1",
        brand="nowlez",
        send_date_ist=today,
    )
    rows = db_session.query(WhatsAppDeliveryLog).filter_by(user_id=u.id).all()
    assert len(rows) == 2


def test_different_users_same_template_same_day_is_allowed(db_session):
    """Each user gets their own daily slot."""
    u1 = _make_user(db_session, phone="+919876543210")
    u2 = _make_user(db_session, phone="+919876543211")
    today = date(2026, 5, 20)
    _insert_row(
        db_session,
        user_id=u1.id,
        template_name="nowlez_tomorrow_hearings_v1",
        brand="nowlez",
        send_date_ist=today,
    )
    _insert_row(
        db_session,
        user_id=u2.id,
        template_name="nowlez_tomorrow_hearings_v1",
        brand="nowlez",
        send_date_ist=today,
    )
    rows = db_session.query(WhatsAppDeliveryLog).filter_by(
        template_name="nowlez_tomorrow_hearings_v1",
    ).all()
    assert len(rows) == 2


def test_insert_on_conflict_do_nothing_returns_zero_on_second_attempt(db_session):
    """The exact pattern the worker uses for its dedup claim: a second
    ON CONFLICT DO NOTHING insert with the same key must return rowcount 0
    rather than raising, so worker code can branch on it cleanly.

    On both Postgres and SQLite the partial unique index requires the
    upsert to include the matching ``WHERE send_date_ist IS NOT NULL``
    predicate. The worker uses ``index_where=`` for this; the test
    mirrors that exact shape.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    u = _make_user(db_session)
    stmt1 = (
        sqlite_insert(WhatsAppDeliveryLog)
        .values(
            user_id=u.id,
            template_name="nowlez_tomorrow_hearings_v1",
            brand="nowlez",
            send_date_ist=date(2026, 5, 20),
            delivery_status="pending",
        )
        .on_conflict_do_nothing(
            index_elements=["user_id", "template_name", "send_date_ist"],
            index_where=WhatsAppDeliveryLog.send_date_ist.isnot(None),
        )
    )
    r1 = db_session.execute(stmt1)
    db_session.commit()
    assert r1.rowcount == 1, "first insert should claim the slot"

    stmt2 = (
        sqlite_insert(WhatsAppDeliveryLog)
        .values(
            user_id=u.id,
            template_name="nowlez_tomorrow_hearings_v1",
            brand="nowlez",
            send_date_ist=date(2026, 5, 20),
            delivery_status="pending",
        )
        .on_conflict_do_nothing(
            index_elements=["user_id", "template_name", "send_date_ist"],
            index_where=WhatsAppDeliveryLog.send_date_ist.isnot(None),
        )
    )
    r2 = db_session.execute(stmt2)
    db_session.commit()
    assert r2.rowcount == 0, (
        "second insert with same dedup key must short-circuit via "
        "ON CONFLICT DO NOTHING, returning rowcount=0"
    )

    # Still exactly one row in the table.
    rows = db_session.query(WhatsAppDeliveryLog).filter_by(user_id=u.id).all()
    assert len(rows) == 1
