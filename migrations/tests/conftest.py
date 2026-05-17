"""Test fixtures for the migrations package.

This conftest ensures:

* The ``migrations`` package is importable — both with the editable install
  (``pip install -e .`` in this directory) and via ``sys.path`` fallback if
  the install hasn't been run.
* An in-memory SQLite ``db_session`` fixture mirrors the data-access test
  pattern (UUID coercion + ``Base.metadata.create_all``) and additionally
  creates the two legacy tables (``_legacy_nowlez_client_cases``,
  ``_legacy_nowlez_case_orders``) that the re-fetch script reads from.
* An autouse ``patch_get_session_in_script`` fixture redirects every
  ``get_session()`` call inside the migration script (and its validator
  sibling) to yield the *same* in-memory session — otherwise the script
  would open a fresh ``SessionFactory()`` against the real ``DATABASE_URL``
  and never see the test's seeded legacy rows.

Why the sys.path dance below:

pytest auto-detects ``rootdir`` by walking up to the git repo
(``C:\\Project3\\shared``) and inserts that path at ``sys.path[0]`` BEFORE
importing this conftest. That makes the outer ``ecourts_client/`` and
``data-access/`` directories — neither of which has ``__init__.py`` — become
empty PEP 420 namespace packages that shadow the editable installs. So we
strip that bad entry, import the real packages, then re-add it (now harmless
because the imported modules are pinned in ``sys.modules``).
"""
from __future__ import annotations

import sqlite3
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

# Step 1: drop the shadow-inducing path entry (if pytest added it).
_SHARED_ROOT = str(Path(__file__).resolve().parents[2])  # C:\Project3\shared
while _SHARED_ROOT in sys.path:
    sys.path.remove(_SHARED_ROOT)

# Step 2: import the real packages. These are editable installs; the shadow
# is now gone so resolution succeeds.
import ecourts_client  # noqa: F401,E402 — pin in sys.modules
import data_access  # noqa: F401,E402 — pin in sys.modules

# Step 3: re-add the shared root so ``import migrations.<module>`` still
# works on machines without ``pip install -e migrations/``. With the
# imported packages pinned, the namespace shadow is harmless.
if _SHARED_ROOT not in sys.path:
    sys.path.append(_SHARED_ROOT)

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from data_access.base import Base  # noqa: E402
import data_access.models  # noqa: E402,F401 — register all models

# Make sqlite3 accept Python ``uuid.UUID`` as bind parameter (mirrors the
# data-access conftest; process-wide and harmless on Postgres).
sqlite3.register_adapter(uuid.UUID, str)


@pytest.fixture
def db_session():
    """In-memory SQLite session with all data-access tables + legacy tables."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE _legacy_nowlez_client_cases ("
            "id INTEGER PRIMARY KEY, "
            "user_id TEXT, "
            "cnr TEXT, "
            "client_id TEXT, "
            "refresh_enabled BOOLEAN, "
            "notes TEXT"
            ")"
        ))
        conn.execute(text(
            "CREATE TABLE _legacy_nowlez_case_orders ("
            "id INTEGER PRIMARY KEY, "
            "client_case_id INTEGER, "
            "order_id TEXT, "
            "file_path TEXT, "
            "page_count INTEGER, "
            "preprocessed BOOLEAN, "
            "preprocessed_at TIMESTAMP, "
            "retry_count INTEGER, "
            "permanently_failed BOOLEAN, "
            "uploaded_at TIMESTAMP"
            ")"
        ))
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def patch_get_session_in_script(monkeypatch, request):
    """Redirect ``get_session()`` calls inside the migration modules to the
    per-test ``db_session``.

    The migration script opens fresh ``get_session()`` contexts in its
    workers; without this patch they'd hit the real (Postgres) engine via
    ``data_access.engine.SessionFactory`` and never see the test's seeded
    legacy rows.

    Conditional on ``db_session`` being requested by the test; pure
    import-smoke tests don't need it.
    """
    if "db_session" not in request.fixturenames:
        return

    session = request.getfixturevalue("db_session")

    @contextmanager
    def _fake_get_session():
        # The migration script and validators both call ``session.commit()``
        # explicitly; yielding the same in-memory session makes that
        # safe — autocommit-on-flush against the shared connection.
        yield session

    # Patch the script-level rebinds for any modules already imported.
    for mod_name in (
        "migrations.refetch_nowlez_cases",
        "migrations.refetch_nowlez_cases_validators",
    ):
        if mod_name in sys.modules:
            monkeypatch.setattr(
                f"{mod_name}.get_session", _fake_get_session, raising=False
            )
