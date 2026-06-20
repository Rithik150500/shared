import os
import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from data_access.base import Base
import data_access.models  # noqa — register all models

# Teach sqlite3 to bind Python uuid.UUID as str (with_variant(String(36)) only
# changes DDL; the driver still needs the adapter). Process-wide; harmless for PG.
sqlite3.register_adapter(uuid.UUID, str)
# Read direction: coerce stored UUID strings back to uuid.UUID objects on fetch.
sqlite3.register_converter("VARCHAR", lambda b: (
    uuid.UUID(b.decode()) if len(b) == 36 and b.count(b"-") == 4 else b.decode()
))


@pytest.fixture(scope="session")
def test_engine():
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL or DATABASE_URL not set")
    engine = create_engine(url, future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def postgresql_session(test_engine):
    """Per-test clean schema."""
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
    """Per-test in-memory SQLite session using with_variant() PG-compat columns.

    Deliberately ignores TEST_DATABASE_URL so Phase-2 unit tests run with no
    Postgres dependency (mirrors data-access conftest's db_session).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"detect_types": sqlite3.PARSE_DECLTYPES},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()
