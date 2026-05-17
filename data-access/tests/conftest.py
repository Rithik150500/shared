import os
import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from data_access.base import Base
import data_access.models  # noqa — register all models


# Teach sqlite3 to bind Python uuid.UUID objects as their str repr. Required by
# the SQLite `db_session` fixture because with_variant(String(36), "sqlite") only
# changes DDL — the bind-parameter coercion still has to be set up at the driver
# layer. Registered globally because sqlite3 adapters are process-wide; harmless
# for the Postgres path (which uses its own UUID type).
sqlite3.register_adapter(uuid.UUID, str)


@pytest.fixture(scope="session")
def test_engine():
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL or DATABASE_URL not set — skipping DB-dependent tests")
    engine = create_engine(url, future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def postgresql_session(test_engine):
    """Per-test clean schema. Slow but simple; optimize later with savepoints if needed."""
    Base.metadata.drop_all(test_engine)
    with test_engine.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    s = Session()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def db_session():
    """Per-test SQLite session using the with_variant() Postgres-compat columns.

    Lets us run DAO tests without a Postgres dependency. If you set
    TEST_DATABASE_URL or DATABASE_URL to a real Postgres URL, the
    `postgresql_session` fixture is the right one to use instead — this
    fixture deliberately ignores those env vars and always uses an in-memory
    SQLite database.
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()
