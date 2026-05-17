"""Shared fixtures for the integration test suite.

Each integration test exercises a slice of the pipeline that spans the
producer (enqueue), the queue (fakeredis), the worker (executed
synchronously via ``Queue.enqueue`` + ``Worker.work(burst=True)`` is
overkill for our purposes — most tests just pop the job kwargs and
invoke ``_do_send_*`` directly), and the DB DAO writes.

The setup here mirrors the in-memory SQLite + fakeredis pattern used by
the unit suite so the integration tests stay hermetic and fast.
"""
from __future__ import annotations

import uuid

import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from data_access.base import Base
import data_access.models  # noqa: F401 — register all models with metadata


@pytest.fixture
def db_session():
    """In-memory SQLite session mirroring the data_access shape."""
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


@pytest.fixture
def fake_redis(monkeypatch):
    """fakeredis + monkey-patch on dispatch.queue._get_redis so the
    enqueue helpers land jobs on the fake connection without hitting
    a real Redis (or even reading WhatsAppConfig)."""
    from whatsapp_delivery.dispatch import queue as q

    fr = fakeredis.FakeRedis()
    monkeypatch.setattr(q, "_get_redis", lambda: fr)
    return fr


@pytest.fixture(autouse=True)
def _meta_env(monkeypatch):
    """Worker functions construct WhatsAppConfig from env on every call.
    Provide stable creds so the construction succeeds in tests that don't
    touch the Meta API at all (e.g. queue-only assertions)."""
    monkeypatch.setenv("META_PHONE_NUMBER_ID", "111")
    monkeypatch.setenv("META_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("META_VERIFY_TOKEN", "ver")
    monkeypatch.setenv("META_APP_SECRET", "sec")
    monkeypatch.delenv("WHATSAPP_NOWLEZ_DISABLED", raising=False)
