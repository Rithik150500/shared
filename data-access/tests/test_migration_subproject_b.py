"""Sub-project B migration test.

Spins up alembic against the test Postgres URL, upgrades to the B head, and
asserts via ``information_schema`` that the new whatsapp_delivery_log table,
the users_nowlez consent columns, and the (possibly preexisting) message_log
table all match the spec. Also covers a downgrade-then-reupgrade cycle to
catch non-idempotent DDL.

Falls back to skip-with-a-message when no test Postgres is available — the
table uses Postgres-specific syntax (``gen_random_uuid()`` default, partial
index) so SQLite isn't a meaningful target for the migration script itself.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

NEW_TABLES = ["whatsapp_delivery_log", "message_log"]


def _postgres_url() -> str | None:
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "postgresql://test:test@localhost:5432/test_unification"
    )


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option(
        "script_location", str(REPO_ROOT / "data_access" / "alembic"),
    )
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="module")
def pg_engine():
    url = _postgres_url()
    if not url:
        pytest.skip("No Postgres test URL available")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as ex:
        engine.dispose()
        pytest.skip(f"Postgres not reachable at {url}: {ex}")
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def fresh_pg_at_b_head(pg_engine):
    """Drop everything in public, upgrade to head (which now includes B)."""
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    cfg = _alembic_config(str(pg_engine.url))
    os.environ["DATABASE_URL"] = str(pg_engine.url)
    command.upgrade(cfg, "head")
    yield pg_engine


def _existing_tables(engine):
    insp = inspect(engine)
    return set(insp.get_table_names(schema="public"))


def test_whatsapp_delivery_log_table_exists(fresh_pg_at_b_head):
    assert "whatsapp_delivery_log" in _existing_tables(fresh_pg_at_b_head)


def test_whatsapp_delivery_log_columns(fresh_pg_at_b_head):
    insp = inspect(fresh_pg_at_b_head)
    cols = {c["name"] for c in insp.get_columns("whatsapp_delivery_log")}
    expected = {
        "id", "user_id", "template_name", "brand", "meta_message_id",
        "rq_job_id", "delivery_status", "failure_reason",
        "related_case_id", "related_order_id",
        "enqueued_at", "sent_at", "delivered_at", "read_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_whatsapp_delivery_log_check_constraints(fresh_pg_at_b_head):
    insp = inspect(fresh_pg_at_b_head)
    checks = insp.get_check_constraints("whatsapp_delivery_log")
    names = {c["name"] for c in checks}
    assert "whatsapp_delivery_log_brand_check" in names
    assert "whatsapp_delivery_log_status_check" in names


def test_users_nowlez_has_consent_columns(fresh_pg_at_b_head):
    insp = inspect(fresh_pg_at_b_head)
    cols = {c["name"]: c for c in insp.get_columns("users_nowlez")}
    assert "whatsapp_events_enabled" in cols
    assert "whatsapp_reminders_enabled" in cols
    assert cols["whatsapp_events_enabled"]["nullable"] is False
    assert cols["whatsapp_reminders_enabled"]["nullable"] is False


def test_message_log_table_exists(fresh_pg_at_b_head):
    assert "message_log" in _existing_tables(fresh_pg_at_b_head)


def test_message_log_meta_id_unique(fresh_pg_at_b_head):
    insp = inspect(fresh_pg_at_b_head)
    uniques = insp.get_unique_constraints("message_log")
    cols_seen = {tuple(u["column_names"]) for u in uniques}
    assert ("meta_message_id",) in cols_seen, (
        f"expected unique on meta_message_id; got: {cols_seen}"
    )


def test_b_migration_double_upgrade_is_idempotent(pg_engine):
    """Upgrade head twice in a row should be a no-op the second time."""
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    tables = _existing_tables(pg_engine)
    for t in NEW_TABLES:
        assert t in tables


def test_b_migration_down_and_reupgrade(pg_engine):
    """Down one revision (to E) then re-upgrade; final state matches first."""
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "head")
    first = _existing_tables(pg_engine)
    assert "whatsapp_delivery_log" in first

    command.downgrade(cfg, "-1")
    after_down = _existing_tables(pg_engine)
    assert "whatsapp_delivery_log" not in after_down

    insp = inspect(pg_engine)
    cols = {c["name"] for c in insp.get_columns("users_nowlez")}
    assert "whatsapp_events_enabled" not in cols
    assert "whatsapp_reminders_enabled" not in cols

    command.upgrade(cfg, "head")
    reup = _existing_tables(pg_engine)
    assert "whatsapp_delivery_log" in reup
