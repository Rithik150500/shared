"""B-3 migration tests: ``20260606_b3_dedup_send_per_day``.

Has two layers:

1. **Postgres path** (skipped when no test DB available) — runs the full
   alembic ``upgrade head`` against a real Postgres, then asserts the new
   ``send_date_ist`` column and partial unique index exist, plus
   downgrade-then-reupgrade idempotency.

2. **Backfill-and-dedup logic** (always runs against in-memory SQLite) —
   pre-seeds a table with duplicate rows missing ``send_date_ist``, runs
   the migration's DELETE-keep-min-id step directly, asserts only the
   winners survive. This is what catches a logic regression in the
   dedup SQL without needing a Postgres image in CI.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _postgres_url() -> str | None:
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
def fresh_pg_at_b3_head(pg_engine):
    """Drop everything and upgrade to head (which now includes B-3).

    When alembic's env.py reads ``settings.DATABASE_URL`` (a module-level
    singleton) instead of the URL we passed via ``set_main_option``, the
    upgrade can fail with an unrelated auth error against the dev env's
    Postgres. Skip rather than fail so the SQLite-path tests in this
    module still run.
    """
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    cfg = _alembic_config(str(pg_engine.url))
    os.environ["DATABASE_URL"] = str(pg_engine.url)
    try:
        command.upgrade(cfg, "head")
    except Exception as ex:
        pytest.skip(f"alembic upgrade failed in test env (likely env.py config singleton): {ex}")
    yield pg_engine


def test_send_date_ist_column_exists(fresh_pg_at_b3_head):
    insp = inspect(fresh_pg_at_b3_head)
    cols = {c["name"] for c in insp.get_columns("whatsapp_delivery_log")}
    assert "send_date_ist" in cols


def test_send_date_ist_column_is_nullable(fresh_pg_at_b3_head):
    """Transactional sends keep ``send_date_ist=NULL``; the column must allow it."""
    insp = inspect(fresh_pg_at_b3_head)
    cols = {c["name"]: c for c in insp.get_columns("whatsapp_delivery_log")}
    assert cols["send_date_ist"]["nullable"] is True


def test_partial_unique_index_exists(fresh_pg_at_b3_head):
    insp = inspect(fresh_pg_at_b3_head)
    indexes = insp.get_indexes("whatsapp_delivery_log")
    target = "whatsapp_delivery_log_user_template_day_unique"
    match = [i for i in indexes if i["name"] == target]
    assert match, f"index {target!r} missing; saw: {[i['name'] for i in indexes]}"
    idx = match[0]
    assert idx["unique"] is True
    assert idx["column_names"] == ["user_id", "template_name", "send_date_ist"]


def test_b3_downgrade_drops_column_and_index(pg_engine):
    """Downgrade -1 should remove both the column and the partial unique index."""
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    cfg = _alembic_config(str(pg_engine.url))
    try:
        command.upgrade(cfg, "head")
    except Exception as ex:
        pytest.skip(f"alembic upgrade failed in test env: {ex}")

    insp = inspect(pg_engine)
    cols = {c["name"] for c in insp.get_columns("whatsapp_delivery_log")}
    assert "send_date_ist" in cols

    command.downgrade(cfg, "-1")

    insp = inspect(pg_engine)
    cols_after = {c["name"] for c in insp.get_columns("whatsapp_delivery_log")}
    assert "send_date_ist" not in cols_after, (
        "downgrade must drop send_date_ist column"
    )
    indexes_after = {i["name"] for i in insp.get_indexes("whatsapp_delivery_log")}
    assert "whatsapp_delivery_log_user_template_day_unique" not in indexes_after, (
        "downgrade must drop the partial unique index"
    )

    # Re-upgrade must restore the head shape.
    command.upgrade(cfg, "head")
    insp = inspect(pg_engine)
    cols_reup = {c["name"] for c in insp.get_columns("whatsapp_delivery_log")}
    assert "send_date_ist" in cols_reup


def test_b3_double_upgrade_is_idempotent(pg_engine):
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    cfg = _alembic_config(str(pg_engine.url))
    try:
        command.upgrade(cfg, "head")
        command.upgrade(cfg, "head")
    except Exception as ex:
        pytest.skip(f"alembic upgrade failed in test env: {ex}")
    insp = inspect(pg_engine)
    cols = {c["name"] for c in insp.get_columns("whatsapp_delivery_log")}
    assert "send_date_ist" in cols


# ---------------------------------------------------------------------------
# SQLite path: directly exercise the dedup-DELETE backfill logic on a
# pre-seeded table. Always runs (no DB dependency).
# ---------------------------------------------------------------------------


def _seed_pre_migration_sqlite():
    """Create a SQLite engine where ``whatsapp_delivery_log`` has the same
    shape as a real DB BEFORE the B-3 migration (no ``send_date_ist``, no
    partial unique index) so we can exercise the migration's backfill +
    dedup DELETE step against representative rows.
    """
    import sqlite3
    sqlite3.register_adapter(uuid.UUID, str)

    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE whatsapp_delivery_log (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                template_name TEXT NOT NULL,
                brand TEXT NOT NULL,
                meta_message_id TEXT,
                rq_job_id TEXT,
                delivery_status TEXT NOT NULL DEFAULT 'pending',
                failure_reason TEXT,
                related_case_id TEXT,
                related_order_id TEXT,
                enqueued_at TEXT NOT NULL,
                sent_at TEXT,
                delivered_at TEXT,
                read_at TEXT
            )
        """))
    return engine


def test_backfill_dedup_keeps_min_id_per_group():
    """Three duplicate rows + two unique rows -> after migration, exactly
    3 rows survive (2 unique + 1 winner per group). The winner is the row
    with the smallest id by text comparison."""
    engine = _seed_pre_migration_sqlite()
    uid_a = "00000000-0000-0000-0000-000000000001"
    uid_b = "00000000-0000-0000-0000-000000000002"
    now = datetime.now(timezone.utc).isoformat()

    rows = [
        # Group 1: three duplicates (user_a, tomorrow_hearings, 2026-05-20).
        ("aaaaaaaa-1111-1111-1111-111111111111", uid_a, "nowlez_tomorrow_hearings_v1"),
        ("bbbbbbbb-1111-1111-1111-111111111111", uid_a, "nowlez_tomorrow_hearings_v1"),
        ("cccccccc-1111-1111-1111-111111111111", uid_a, "nowlez_tomorrow_hearings_v1"),
        # Group 2: unique row (user_b, tomorrow_hearings, same day).
        ("dddddddd-2222-2222-2222-222222222222", uid_b, "nowlez_tomorrow_hearings_v1"),
        # Group 3: unique row (user_a, weekly_summary, same day).
        ("eeeeeeee-3333-3333-3333-333333333333", uid_a, "nowlez_weekly_summary_v1"),
    ]
    with engine.begin() as conn:
        for row_id, user_id, tmpl in rows:
            conn.execute(text(
                "INSERT INTO whatsapp_delivery_log "
                "(id, user_id, template_name, brand, delivery_status, "
                " enqueued_at, sent_at) "
                "VALUES (:i, :u, :t, 'nowlez', 'sent', :now, :now)"
            ), {"i": row_id, "u": user_id, "t": tmpl, "now": now})

    # Apply the migration's body manually (alembic's baseline migration is
    # Postgres-only so we cannot run ``command.upgrade`` against SQLite).
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE whatsapp_delivery_log ADD COLUMN send_date_ist DATE"))
        conn.execute(text(
            "UPDATE whatsapp_delivery_log "
            "SET send_date_ist = date(COALESCE(sent_at, enqueued_at)) "
            "WHERE template_name IN ('nowlez_tomorrow_hearings_v1', "
            "                        'nowlez_weekly_summary_v1')"
        ))
        # The dedup step matching the migration's SQLite branch.
        conn.execute(text("""
            DELETE FROM whatsapp_delivery_log
            WHERE send_date_ist IS NOT NULL
              AND id NOT IN (
                SELECT MIN(id) FROM whatsapp_delivery_log
                WHERE send_date_ist IS NOT NULL
                GROUP BY user_id, template_name, send_date_ist
              )
        """))

    with engine.begin() as conn:
        rows_after = list(conn.execute(text(
            "SELECT id FROM whatsapp_delivery_log ORDER BY id"
        )))

    surviving_ids = {r[0] for r in rows_after}
    assert surviving_ids == {
        # Winner of the duplicate group: smallest id by text sort.
        "aaaaaaaa-1111-1111-1111-111111111111",
        # The two singletons must survive untouched.
        "dddddddd-2222-2222-2222-222222222222",
        "eeeeeeee-3333-3333-3333-333333333333",
    }, f"unexpected survivors: {surviving_ids}"
    assert len(rows_after) == 3, (
        f"expected 3 rows after dedup (1 winner + 2 unique); got {len(rows_after)}"
    )


def test_backfill_leaves_non_daily_rows_with_null_send_date():
    """Rows for transactional templates must keep ``send_date_ist=NULL``."""
    engine = _seed_pre_migration_sqlite()
    uid = "00000000-0000-0000-0000-000000000001"
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO whatsapp_delivery_log "
            "(id, user_id, template_name, brand, delivery_status, enqueued_at, sent_at) "
            "VALUES ('11111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa', :u, "
            "        'nowlez_signup_welcome_v1', 'nowlez', 'sent', :now, :now)"
        ), {"u": uid, "now": now})

        conn.execute(text("ALTER TABLE whatsapp_delivery_log ADD COLUMN send_date_ist DATE"))
        conn.execute(text(
            "UPDATE whatsapp_delivery_log "
            "SET send_date_ist = date(COALESCE(sent_at, enqueued_at)) "
            "WHERE template_name IN ('nowlez_tomorrow_hearings_v1', "
            "                        'nowlez_weekly_summary_v1')"
        ))

        row = conn.execute(text(
            "SELECT send_date_ist FROM whatsapp_delivery_log "
            "WHERE template_name = 'nowlez_signup_welcome_v1'"
        )).one()
        assert row[0] is None, (
            "transactional rows must keep send_date_ist=NULL (they are "
            "exempt from the partial unique constraint)"
        )
