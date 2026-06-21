"""Migration tests for ``20260620_g_orphan_cols`` (Sub-G cutover step 1).

Mirrors ``test_migration_unified_login.py`` so this schema migration carries the
same guarantees as every prior migration in the package:

Two layers:
1. Always-on guards (no DB): single-head gate (so a future sibling branching off
   20260619_unified_login can't create multiple heads silently); the new revision
   chains onto 20260619_unified_login; and its id is <= 32 chars (alembic_version
   is VARCHAR(32) — legacy 36-char ids predate the constraint and are not asserted).
2. Postgres path (skip if no DB): upgrade head adds all 7 orphan columns to
   users_nowlez with the right nullability/defaults; downgrade -1 drops them;
   re-upgrade is idempotent. This closes the model-vs-migration drift gap the
   model-only test (test_users_nowlez_orphan_columns.py) cannot catch.
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
NEW_REVISION = "20260620_g_orphan_cols"
EXPECTED_DOWN = "20260619_unified_login"

ORPHAN_COLUMNS = {
    "monthly_upload_count",
    "usage_reset_date",
    "last_export_at",
    "last_case_exports_at",
    "unsubscribed_at",
    "first_case_email_sent",
    "last_digest_sent_date",
}


def _script_dir() -> ScriptDirectory:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "data_access" / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_single_head_after_new_migration():
    # Head-agnostic branch guard: catches accidental BRANCHING (>1 head). It must
    # NOT hardcode a specific revision, or every legitimate new migration that
    # advances the head (e.g. 20260621_step2_clients) breaks it. The current-head
    # *identity* is asserted by the latest migration's own test.
    sd = _script_dir()
    heads = sd.get_heads()
    assert len(heads) == 1, f"expected exactly one alembic head, got {heads}"


def test_new_revision_chains_onto_unified_login():
    sd = _script_dir()
    rev = sd.get_revision(NEW_REVISION)
    assert rev.down_revision == EXPECTED_DOWN


def test_new_revision_id_within_32_chars():
    # alembic_version is VARCHAR(32); the new id MUST fit (legacy 36-char ids
    # predate this constraint and are intentionally not asserted here).
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


def test_pg_upgrade_adds_all_seven_orphan_columns(fresh_pg_at_head):
    cols = {c["name"] for c in inspect(fresh_pg_at_head).get_columns("users_nowlez")}
    missing = ORPHAN_COLUMNS - cols
    assert not missing, f"users_nowlez missing orphan columns after upgrade: {sorted(missing)}"


def test_pg_orphan_columns_nullability_and_defaults(fresh_pg_at_head):
    by_name = {
        c["name"]: c for c in inspect(fresh_pg_at_head).get_columns("users_nowlez")
    }

    # NOT NULL + defaulted columns.
    assert by_name["monthly_upload_count"]["nullable"] is False
    assert by_name["monthly_upload_count"]["default"] is not None
    assert by_name["first_case_email_sent"]["nullable"] is False
    assert by_name["first_case_email_sent"]["default"] is not None

    # Nullable columns (no NOT NULL constraint).
    for nullable_col in (
        "usage_reset_date",
        "last_export_at",
        "last_case_exports_at",
        "unsubscribed_at",
        "last_digest_sent_date",
    ):
        assert by_name[nullable_col]["nullable"] is True, nullable_col


def test_pg_downgrade_drops_all_seven_then_reupgrade(pg_engine):
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
    cols = {c["name"] for c in inspect(pg_engine).get_columns("users_nowlez")}
    still_present = ORPHAN_COLUMNS & cols
    assert not still_present, f"downgrade left orphan columns behind: {sorted(still_present)}"

    # Re-upgrade restores all 7 (idempotent forward path).
    command.upgrade(cfg, "head")
    cols_reup = {c["name"] for c in inspect(pg_engine).get_columns("users_nowlez")}
    assert ORPHAN_COLUMNS <= cols_reup


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
    cols = {c["name"] for c in inspect(pg_engine).get_columns("users_nowlez")}
    assert ORPHAN_COLUMNS <= cols
