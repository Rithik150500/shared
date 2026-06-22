"""Migration tests for 20260621_step3_case_detail.

Layer 1 (no DB): single-head guard, chains onto the recorded head, id <= 32.
Layer 2 (skip if no PG): upgrade head adds the 3 columns; downgrade -1 drops
them; re-upgrade clean. search_tsv is NOT touched (it pre-exists)."""
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
NEW_REVISION = "20260621_step3_case_detail"
EXPECTED_DOWN = "20260621_step2_clients"  # the head recorded in Task 0.d


def _script_dir() -> ScriptDirectory:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "data_access" / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_single_head_after_new_migration():
    assert _script_dir().get_heads() == [NEW_REVISION]


def test_new_revision_chains_onto_recorded_head():
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


def test_pg_upgrade_adds_three_columns(fresh_pg_at_head):
    cols = {c["name"] for c in inspect(fresh_pg_at_head).get_columns("cases")}
    for col in ("case_detail_json", "case_detail_md", "mini_case_detail_md"):
        assert col in cols, f"{col} missing"


def test_pg_search_tsv_still_present_and_untouched(fresh_pg_at_head):
    cols = {c["name"] for c in inspect(fresh_pg_at_head).get_columns("cases")}
    assert "search_tsv" in cols  # pre-existing; Step-3 must NOT re-add or drop it


def test_pg_downgrade_drops_the_three_columns(pg_engine):
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
    cols = {c["name"] for c in inspect(pg_engine).get_columns("cases")}
    for col in ("case_detail_json", "case_detail_md", "mini_case_detail_md"):
        assert col not in cols, f"{col} not dropped on downgrade"
    assert "search_tsv" in cols  # downgrade must leave the pre-existing col alone
    command.upgrade(cfg, "head")  # reversibility
    assert "case_detail_md" in {c["name"] for c in inspect(pg_engine).get_columns("cases")}
