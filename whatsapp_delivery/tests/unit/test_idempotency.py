"""Tests for ``whatsapp_delivery.idempotency.claim_message``.

The claim function uses INSERT ON CONFLICT DO NOTHING keyed on
``meta_message_id`` so an inbound webhook dispatches exactly once even
when Meta retries the same delivery for 24h. The test fixture mirrors
the in-memory SQLite shape used by the webhook tests so the test runs
against the same SQLAlchemy metadata the production migration sets up.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from data_access.base import Base
import data_access.models  # noqa: F401 — register all models with metadata

from whatsapp_delivery.idempotency import claim_message


@pytest.fixture
def db_session():
    """Per-test SQLite session that mirrors the data_access shape."""
    import sqlite3
    sqlite3.register_adapter(uuid.UUID, str)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def test_first_sighting_returns_true(db_session):
    """First time we see a meta_message_id → claim succeeds."""
    user_id = uuid.uuid4()
    claimed = claim_message(
        db_session,
        meta_message_id="wamid.first-001",
        user_id=user_id,
    )
    assert claimed is True


def test_second_sighting_returns_false(db_session):
    """Meta retries us with the same meta_message_id → claim is a no-op."""
    user_id = uuid.uuid4()
    first = claim_message(
        db_session,
        meta_message_id="wamid.dup-001",
        user_id=user_id,
    )
    assert first is True

    # Same meta_message_id, even with a different user_id — the unique
    # constraint is on meta_message_id alone.
    second = claim_message(
        db_session,
        meta_message_id="wamid.dup-001",
        user_id=uuid.uuid4(),
    )
    assert second is False


def test_distinct_meta_ids_both_claimed(db_session):
    """Two different webhooks both succeed (no cross-talk)."""
    user_id = uuid.uuid4()
    assert claim_message(
        db_session,
        meta_message_id="wamid.distinct-A",
        user_id=user_id,
    ) is True
    assert claim_message(
        db_session,
        meta_message_id="wamid.distinct-B",
        user_id=user_id,
    ) is True


def test_claim_persists_user_id(db_session):
    """The user_id passed in is persisted on the new row."""
    from data_access.models.whatsapp import MessageLog

    user_id = uuid.uuid4()
    claim_message(
        db_session,
        meta_message_id="wamid.persist-check",
        user_id=user_id,
    )

    row = (
        db_session.query(MessageLog)
        .filter_by(meta_message_id="wamid.persist-check")
        .one()
    )
    # SQLite stores UUID as str (registered adapter) — accept either str
    # or UUID-equality.
    assert str(row.user_id) == str(user_id)
