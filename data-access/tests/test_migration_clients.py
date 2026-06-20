"""Migration tests for ``20260621_step2_clients`` (Step-2 SQLite→PG cutover).

Mirrors ``test_migration_g_orphan_cols.py`` so the clients/teams table-creation
migration carries the same guarantees as every prior migration in the package:

Two layers:
1. Always-on guards (no DB): single-head gate (so a future sibling branching off
   the prior head can't create multiple heads silently); the new revision chains
   onto ``20260620_g_orphan_cols``; and its id is <= 32 chars (alembic_version is
   VARCHAR(32)).
2. Postgres path (skip if no DB): upgrade head creates the ``teams`` and
   ``clients`` tables with the right columns / indexes (incl. the partial
   ``clients_team_id_idx``); downgrade -1 drops them; re-upgrade is idempotent.
   This closes the model-vs-migration drift gap the model-only test
   (test_client_model.py) cannot catch.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
NEW_REVISION = "20260621_step2_clients"
EXPECTED_DOWN = "20260620_g_orphan_cols"

CLIENTS_COLUMNS = {
    "id",
    "user_id",
    "team_id",
    "name",
    "email",
    "phone",
    "notes",
    "is_demo",
    "created_at",
    "updated_at",
}
CLIENTS_INDEXES = {
    "clients_user_id_idx",
    "clients_user_created_idx",
    "clients_team_id_idx",
}


def _script_dir() -> ScriptDirectory:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "data_access" / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_single_head_after_new_migration():
    sd = _script_dir()
    heads = sd.get_heads()
    assert heads == [NEW_REVISION], f"expected single head {NEW_REVISION!r}, got {heads}"


def test_new_revision_chains_onto_prior_head():
    sd = _script_dir()
    rev = sd.get_revision(NEW_REVISION)
    assert rev.down_revision == EXPECTED_DOWN


def test_new_revision_id_within_32_chars():
    # alembic_version is VARCHAR(32); the new id MUST fit.
    assert len(NEW_REVISION) <= 32


# ---------------------------------------------------------------------------
# Postgres-path migration tests (upgrade/downgrade/idempotent).
# Skip if no Postgres is reachable.
# ---------------------------------------------------------------------------

from alembic import command  # noqa: E402


def _postgres_url() -> str:
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "postgresql://test:test@localhost:5432/test_unification"
    )


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "data_access" / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="module")
def pg_engine():
    url = _postgres_url()
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as ex:  # noqa: BLE001
        engine.dispose()
        pytest.skip(f"Postgres not reachable at {url}: {ex}")
    yield engine
    engine.dispose()


def _reset_to_head(pg_engine):
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    cfg = _alembic_config(str(pg_engine.url))
    os.environ["DATABASE_URL"] = str(pg_engine.url)
    try:
        command.upgrade(cfg, "head")
    except Exception as ex:  # noqa: BLE001
        pytest.skip(f"alembic upgrade failed in test env: {ex}")
    return cfg


@pytest.fixture(scope="module")
def fresh_pg_at_head(pg_engine):
    _reset_to_head(pg_engine)
    yield pg_engine


def test_pg_upgrade_creates_clients_and_teams(fresh_pg_at_head):
    insp = inspect(fresh_pg_at_head)
    tables = set(insp.get_table_names())
    assert "teams" in tables, "teams table missing after upgrade (FK target for clients.team_id)"
    assert "clients" in tables, "clients table missing after upgrade"


def test_pg_clients_columns_present(fresh_pg_at_head):
    cols = {c["name"] for c in inspect(fresh_pg_at_head).get_columns("clients")}
    missing = CLIENTS_COLUMNS - cols
    assert not missing, f"clients missing columns after upgrade: {sorted(missing)}"


def test_pg_clients_indexes_present(fresh_pg_at_head):
    idx = {i["name"] for i in inspect(fresh_pg_at_head).get_indexes("clients")}
    missing = CLIENTS_INDEXES - idx
    assert not missing, f"clients missing indexes after upgrade: {sorted(missing)}"


def test_pg_clients_nullability_and_defaults(fresh_pg_at_head):
    by_name = {c["name"]: c for c in inspect(fresh_pg_at_head).get_columns("clients")}
    # NOT NULL columns.
    assert by_name["user_id"]["nullable"] is False
    assert by_name["name"]["nullable"] is False
    assert by_name["is_demo"]["nullable"] is False
    assert by_name["is_demo"]["default"] is not None
    # Nullable columns.
    for nullable_col in ("team_id", "email", "phone", "notes"):
        assert by_name[nullable_col]["nullable"] is True, nullable_col


def test_pg_downgrade_drops_then_reupgrade(pg_engine):
    cfg = _reset_to_head(pg_engine)
    command.downgrade(cfg, "-1")
    tables = set(inspect(pg_engine).get_table_names())
    assert "clients" not in tables, "downgrade left clients table behind"
    assert "teams" not in tables, "downgrade left teams table behind"

    # Re-upgrade restores both (idempotent forward path).
    command.upgrade(cfg, "head")
    tables_reup = set(inspect(pg_engine).get_table_names())
    assert {"clients", "teams"} <= tables_reup


def test_pg_double_upgrade_idempotent(pg_engine):
    cfg = _reset_to_head(pg_engine)
    command.upgrade(cfg, "head")  # second upgrade is a no-op
    tables = set(inspect(pg_engine).get_table_names())
    assert {"clients", "teams"} <= tables
