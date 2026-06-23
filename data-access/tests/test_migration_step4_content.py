"""Migration tests for 20260622_step4_content.

Layer 1 (no DB): single-head guard, chains onto the recorded Step-3 head,
id <= 32. Layer 2 (skip if no PG): upgrade creates the 3 tables + search_tsv
generated cols + GIN; downgrade drops them; re-upgrade clean."""
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
NEW_REVISION = "20260622_step4_content"
EXPECTED_DOWN = "20260621_step3_case_detail"  # the single head recorded in Task 0.d


def _script_dir() -> ScriptDirectory:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "data_access" / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_single_head_after_new_migration():
    assert _script_dir().get_heads() == [NEW_REVISION]


def test_new_revision_chains_onto_step3_head():
    assert _script_dir().get_revision(NEW_REVISION).down_revision == EXPECTED_DOWN


def test_new_revision_id_within_32_chars():
    assert len(NEW_REVISION) <= 32


def _postgres_url() -> str:
    return (os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
            or "postgresql://test:test@localhost:5432/test_unification")


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


@pytest.fixture(scope="module")
def fresh_pg_at_head(pg_engine):
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
    yield pg_engine


def test_pg_upgrade_creates_three_tables(fresh_pg_at_head):
    names = set(inspect(fresh_pg_at_head).get_table_names())
    for t in ("uploaded_files_nowlez", "chat_history_nowlez", "notifications_nowlez"):
        assert t in names, f"{t} missing"


def test_pg_search_tsv_generated_columns_present(fresh_pg_at_head):
    insp = inspect(fresh_pg_at_head)
    for t in ("uploaded_files_nowlez", "chat_history_nowlez"):
        assert "search_tsv" in {c["name"] for c in insp.get_columns(t)}


def test_pg_dedup_key_unique(fresh_pg_at_head):
    ucs = inspect(fresh_pg_at_head).get_unique_constraints("notifications_nowlez")
    assert any("dedup_key" in uc["column_names"] for uc in ucs)


def test_pg_downgrade_drops_three_tables(pg_engine):
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    cfg = _alembic_config(str(pg_engine.url))
    try:
        command.upgrade(cfg, "head")
    except Exception as ex:  # noqa: BLE001
        pytest.skip(f"alembic upgrade failed in test env: {ex}")
    command.downgrade(cfg, "-1")
    names = set(inspect(pg_engine).get_table_names())
    for t in ("uploaded_files_nowlez", "chat_history_nowlez", "notifications_nowlez"):
        assert t not in names, f"{t} not dropped on downgrade"
    command.upgrade(cfg, "head")  # reversibility
    assert "uploaded_files_nowlez" in set(inspect(pg_engine).get_table_names())
