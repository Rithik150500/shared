"""Tests for the shared WhatsApp idempotency + delivery-log SQLAlchemy models."""
from __future__ import annotations

from data_access.models.whatsapp import MessageLog, WhatsAppDeliveryLog


def test_message_log_columns():
    cols = {c.name for c in MessageLog.__table__.columns}
    assert {"id", "meta_message_id", "user_phone", "received_at"}.issubset(cols)


def test_message_log_meta_id_unique():
    # The model declares a single-column unique on meta_message_id either via a
    # UniqueConstraint in __table_args__ or via the column-level unique=True.
    table = MessageLog.__table__

    unique_cols = set()
    for con in table.constraints:
        if con.__class__.__name__ == "UniqueConstraint":
            unique_cols.add(tuple(c.name for c in con.columns))
    for idx in table.indexes:
        if idx.unique:
            unique_cols.add(tuple(c.name for c in idx.columns))

    assert any("meta_message_id" in cols for cols in unique_cols), (
        f"expected a unique on meta_message_id; got {unique_cols}"
    )


def test_whatsapp_delivery_log_columns():
    cols = {c.name for c in WhatsAppDeliveryLog.__table__.columns}
    expected = {
        "id", "user_id", "template_name", "brand", "meta_message_id",
        "rq_job_id", "delivery_status", "failure_reason",
        "related_case_id", "related_order_id",
        "enqueued_at", "sent_at", "delivered_at", "read_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_whatsapp_delivery_log_brand_check_constraint():
    table = WhatsAppDeliveryLog.__table__
    names = {con.name for con in table.constraints if con.name}
    assert "whatsapp_delivery_log_brand_check" in names


def test_whatsapp_delivery_log_status_check_constraint():
    table = WhatsAppDeliveryLog.__table__
    names = {con.name for con in table.constraints if con.name}
    assert "whatsapp_delivery_log_status_check" in names
