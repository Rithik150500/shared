import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from data_access.base import Base
import data_access.models  # noqa — register all models


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
