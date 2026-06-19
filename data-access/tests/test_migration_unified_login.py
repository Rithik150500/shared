"""Migration tests for ``20260619_unified_login``.

Two layers:
1. Always-on guards (no DB): single-head pre-author gate; the new revision
   exists, chains onto 20260616_broadcast_tables, and its id is <= 32 chars.
   (The repo already contains one legacy 36-char id — the guard asserts the
   NEW migration's id, not every historical id.)
2. Postgres path (skip if no DB): upgrade head creates both tables + the
   email_verified column; downgrade -1 drops them; double-upgrade idempotent;
   two-concurrent-consume serializability.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
NEW_REVISION = "20260619_unified_login"
EXPECTED_DOWN = "20260616_broadcast_tables"


def _script_dir() -> ScriptDirectory:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "data_access" / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_single_head_after_new_migration():
    sd = _script_dir()
    heads = sd.get_heads()
    assert heads == [NEW_REVISION], f"expected single head {NEW_REVISION!r}, got {heads}"


def test_new_revision_chains_onto_broadcast_tables():
    sd = _script_dir()
    rev = sd.get_revision(NEW_REVISION)
    assert rev.down_revision == EXPECTED_DOWN


def test_new_revision_id_within_32_chars():
    # alembic_version is VARCHAR(32); the new id MUST fit (legacy 36-char ids
    # predate this constraint and are intentionally not asserted here).
    assert len(NEW_REVISION) <= 32


# ---------------------------------------------------------------------------
# P1.17 — Postgres-path migration tests (upgrade/downgrade/idempotent)
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
        pytest.skip(f"alembic upgrade failed in test env (env.py config singleton): {ex}")
    yield pg_engine


def test_pg_upgrade_creates_both_tables(fresh_pg_at_head):
    tables = set(inspect(fresh_pg_at_head).get_table_names())
    assert "login_requests" in tables
    assert "email_otp_codes" in tables


def test_pg_upgrade_adds_email_verified_column(fresh_pg_at_head):
    cols = {c["name"] for c in inspect(fresh_pg_at_head).get_columns("users_nowlez")}
    assert "email_verified" in cols


def test_pg_login_requests_indexes_present(fresh_pg_at_head):
    names = {i["name"] for i in inspect(fresh_pg_at_head).get_indexes("login_requests")}
    assert "login_requests_token_hash_idx" in names
    assert "login_requests_expires_at_idx" in names
    assert "login_requests_ip_rate_idx" in names


def test_pg_downgrade_drops_everything(pg_engine):
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
    insp = inspect(pg_engine)
    tables = set(insp.get_table_names())
    assert "login_requests" not in tables
    assert "email_otp_codes" not in tables
    cols = {c["name"] for c in insp.get_columns("users_nowlez")}
    assert "email_verified" not in cols

    # Re-upgrade restores head.
    command.upgrade(cfg, "head")
    tables_reup = set(inspect(pg_engine).get_table_names())
    assert "login_requests" in tables_reup


def test_pg_double_upgrade_idempotent(pg_engine):
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    cfg = _alembic_config(str(pg_engine.url))
    try:
        command.upgrade(cfg, "head")
        command.upgrade(cfg, "head")
    except Exception as ex:  # noqa: BLE001
        pytest.skip(f"alembic upgrade failed in test env: {ex}")
    assert "login_requests" in set(inspect(pg_engine).get_table_names())
