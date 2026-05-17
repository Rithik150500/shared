"""Sub-project E migration test.

Spins up alembic against the test Postgres URL (or `TEST_DATABASE_URL`),
upgrades to the new head, and asserts via `information_schema` that the six
new billing tables, the `users_nowlez` mods, and the `users_munshi` mods all
exist with the right shape. Also checks idempotency (upgrade twice + a
downgrade-then-reupgrade cycle).

Falls back to skip-with-a-message when no test Postgres is available — the
schema modifications use Postgres-specific syntax (`ALTER COLUMN ... DROP NOT
NULL`, `::date` cast, partial indexes) so SQLite isn't a meaningful target
for the migration itself.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

NEW_TABLES = [
    "subscriptions",
    "payment_events",
    "coupon_codes",
    "referrals",
    "case_billing_periods",
    "munshi_invoices",
]


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
def fresh_pg_at_e_head(pg_engine):
    """Drop everything in the public schema, upgrade to head (which includes E)."""
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    cfg = _alembic_config(str(pg_engine.url))
    # Tells env.py to not re-read sqlalchemy.url from settings; the cfg url wins
    # because env.py calls config.set_main_option(...) anyway, but we set it
    # for completeness.
    os.environ["DATABASE_URL"] = str(pg_engine.url)
    command.upgrade(cfg, "head")
    yield pg_engine


def _existing_tables(engine):
    insp = inspect(engine)
    return set(insp.get_table_names(schema="public"))


def test_all_new_tables_exist(fresh_pg_at_e_head):
    tables = _existing_tables(fresh_pg_at_e_head)
    for t in NEW_TABLES:
        assert t in tables, f"missing table: {t} (have: {sorted(tables)})"


def test_subscriptions_has_expected_columns(fresh_pg_at_e_head):
    insp = inspect(fresh_pg_at_e_head)
    cols = {c["name"] for c in insp.get_columns("subscriptions")}
    expected = {
        "id", "user_id", "tier", "billing_cycle",
        "razorpay_subscription_id", "razorpay_customer_id",
        "status", "intro_promo_state", "referral_state",
        "period_start", "period_end", "grace_expires_at", "cancel_at",
        "created_at", "updated_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_payment_events_has_expected_columns(fresh_pg_at_e_head):
    insp = inspect(fresh_pg_at_e_head)
    cols = {c["name"] for c in insp.get_columns("payment_events")}
    expected = {
        "id", "razorpay_event_id", "event_type", "product", "payload",
        "processed_at", "created_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_coupon_codes_has_expected_columns(fresh_pg_at_e_head):
    insp = inspect(fresh_pg_at_e_head)
    cols = {c["name"] for c in insp.get_columns("coupon_codes")}
    expected = {
        "code", "percent_off", "max_redemptions", "redemptions",
        "expires_at", "created_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_referrals_has_expected_columns(fresh_pg_at_e_head):
    insp = inspect(fresh_pg_at_e_head)
    cols = {c["name"] for c in insp.get_columns("referrals")}
    expected = {
        "id", "referrer_user_id", "referred_user_id", "state",
        "referred_subscription_id", "expires_at", "created_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_case_billing_periods_has_expected_columns(fresh_pg_at_e_head):
    insp = inspect(fresh_pg_at_e_head)
    cols = {c["name"] for c in insp.get_columns("case_billing_periods")}
    expected = {
        "id", "user_id", "case_id", "period_start", "period_end", "created_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_munshi_invoices_has_expected_columns(fresh_pg_at_e_head):
    insp = inspect(fresh_pg_at_e_head)
    cols = {c["name"] for c in insp.get_columns("munshi_invoices")}
    expected = {
        "id", "user_id", "razorpay_invoice_id", "cycle_start", "cycle_end",
        "case_count", "amount_paise", "status", "due_at", "paid_at",
        "grace_expires_at", "suspended_at", "created_at", "updated_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_users_nowlez_tier_is_nullable(fresh_pg_at_e_head):
    insp = inspect(fresh_pg_at_e_head)
    tier_col = next(c for c in insp.get_columns("users_nowlez") if c["name"] == "tier")
    assert tier_col["nullable"] is True, (
        f"users_nowlez.tier should be nullable after E migration; got: {tier_col}"
    )


def test_users_nowlez_has_trial_columns(fresh_pg_at_e_head):
    insp = inspect(fresh_pg_at_e_head)
    cols = {c["name"]: c for c in insp.get_columns("users_nowlez")}
    assert "trial_started_at" in cols
    assert "trial_ends_at" in cols


def test_users_munshi_anniversary_present_and_not_null(fresh_pg_at_e_head):
    insp = inspect(fresh_pg_at_e_head)
    cols = {c["name"]: c for c in insp.get_columns("users_munshi")}
    assert "billing_anniversary_date" in cols
    assert cols["billing_anniversary_date"]["nullable"] is False, (
        f"billing_anniversary_date should be NOT NULL post-migration; "
        f"got: {cols['billing_anniversary_date']}"
    )


def test_munshi_invoices_unique_constraint(fresh_pg_at_e_head):
    insp = inspect(fresh_pg_at_e_head)
    uniques = insp.get_unique_constraints("munshi_invoices")
    names = {u["name"] for u in uniques}
    assert "munshi_invoices_user_cycle_unique" in names, (
        f"expected munshi_invoices_user_cycle_unique; got: {names}"
    )


def test_referrals_referred_user_unique(fresh_pg_at_e_head):
    insp = inspect(fresh_pg_at_e_head)
    uniques = insp.get_unique_constraints("referrals")
    names = {u["name"] for u in uniques}
    assert "referrals_referred_unique" in names


def test_payment_events_razorpay_event_id_unique(fresh_pg_at_e_head):
    insp = inspect(fresh_pg_at_e_head)
    uniques = insp.get_unique_constraints("payment_events")
    cols_seen = {tuple(u["column_names"]) for u in uniques}
    assert ("razorpay_event_id",) in cols_seen, (
        f"expected unique on razorpay_event_id; got: {cols_seen}"
    )


def test_migration_idempotent_double_upgrade(pg_engine):
    """upgrade head twice in a row should not raise."""
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "head")
    # Second upgrade is a no-op because we're already at head.
    command.upgrade(cfg, "head")
    # State should match the first-upgrade state.
    assert set(NEW_TABLES).issubset(_existing_tables(pg_engine))


def test_migration_down_and_reupgrade(pg_engine):
    """downgrade past the E revision then re-upgrade to head; state matches.

    The test downgrades directly to the previous E parent (``0002_add_case_tables``)
    by target revision instead of by step-count — since sub-project B now sits
    on top of E, a ``-1`` step would only undo B and leave E in place. Targeting
    by revision id keeps the test's intent (E was reverted) stable as more
    revisions land on top.
    """
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "head")
    tables_after_first = _existing_tables(pg_engine)
    assert set(NEW_TABLES).issubset(tables_after_first)

    # Downgrade to the revision just before E (0002_add_case_tables).
    command.downgrade(cfg, "0002_add_case_tables")
    tables_after_down = _existing_tables(pg_engine)
    for t in NEW_TABLES:
        assert t not in tables_after_down, (
            f"{t} should be gone after downgrade; have: {sorted(tables_after_down)}"
        )
    # users_munshi.billing_anniversary_date should also be gone now.
    insp = inspect(pg_engine)
    munshi_cols = {c["name"] for c in insp.get_columns("users_munshi")}
    assert "billing_anniversary_date" not in munshi_cols

    # Re-upgrade and re-check.
    command.upgrade(cfg, "head")
    tables_after_reup = _existing_tables(pg_engine)
    assert tables_after_first == tables_after_reup, (
        f"table set differs after down+reup\n"
        f"first: {sorted(tables_after_first)}\n"
        f"reup:  {sorted(tables_after_reup)}"
    )
