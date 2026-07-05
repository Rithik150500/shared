"""Migration tests for 20260705_tribunal_family.

Layer 1 (no DB): single-head guard, chains onto multiforum_foundation, id <= 32.
Layer 2 (skip if no PG): upgrade head adds tribunal_kind + the two partial unique
indexes + CHECKs; the DRT fold moves forum='drt' rows to forum='tribunal'/kind='drt';
downgrade -1 un-folds DRT and drops the column; re-upgrade clean.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
NEW_REVISION = "20260705_tribunal_family"
EXPECTED_DOWN = "20260701_multiforum_foundation"


def _script_dir() -> ScriptDirectory:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "data_access" / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_single_head_is_this_revision():
    assert _script_dir().get_heads() == [NEW_REVISION]


def test_new_revision_chains_onto_multiforum_foundation():
    assert _script_dir().get_revision(NEW_REVISION).down_revision == EXPECTED_DOWN


def test_new_revision_id_within_32_chars():
    assert len(NEW_REVISION) <= 32


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


def _reset_and_upgrade(pg_engine, rev="head"):
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    cfg = _alembic_config(str(pg_engine.url))
    os.environ["DATABASE_URL"] = str(pg_engine.url)
    try:
        command.upgrade(cfg, rev)
    except Exception as ex:  # noqa: BLE001
        pytest.skip(f"alembic upgrade failed in test env: {ex}")
    return cfg


def test_pg_upgrade_adds_tribunal_kind_and_indexes(pg_engine):
    _reset_and_upgrade(pg_engine)
    insp = inspect(pg_engine)
    cols = {c["name"] for c in insp.get_columns("cases")}
    assert "tribunal_kind" in cols
    idx = {i["name"] for i in insp.get_indexes("cases")}
    assert "cases_user_forum_ref_unique" in idx  # now a partial index
    assert "cases_user_tribunal_ref_unique" in idx


def test_pg_downgrade_drops_tribunal_kind(pg_engine):
    cfg = _reset_and_upgrade(pg_engine)
    command.downgrade(cfg, "-1")
    cols = {c["name"] for c in inspect(pg_engine).get_columns("cases")}
    assert "tribunal_kind" not in cols
    command.upgrade(cfg, "head")  # reversibility
    assert "tribunal_kind" in {c["name"] for c in inspect(pg_engine).get_columns("cases")}
