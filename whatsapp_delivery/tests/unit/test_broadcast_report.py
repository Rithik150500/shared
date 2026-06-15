"""Tests for whatsapp_delivery.tools.broadcast_report.summarize.

Seeds an in-memory SQLite broadcast ledger with a realistic mix of statuses
(sent, delivered, read, failed/131026, failed/131049, failed/other) and
asserts that ``summarize`` returns correct counts, percentages, and error-code
breakdowns.

Also tests the divide-by-zero guard for an empty campaign.
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import data_access.models  # noqa: F401 — register all models
from data_access.base import Base
from data_access.daos import broadcast_dao
from whatsapp_delivery.tools.broadcast_report import summarize

# ---------------------------------------------------------------------------
# SQLite in-memory fixture
# ---------------------------------------------------------------------------

sqlite3.register_adapter(uuid.UUID, str)


@pytest.fixture()
def db_session():
    """Per-test in-memory SQLite session with all tables created."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

_CAMPAIGN = "report_test_campaign"


def _seed(
    session,
    *,
    wa_digits: str,
    final_status: str,
    error_code: int | None = None,
    tier: str = "T1",
) -> None:
    """Insert a broadcast row and advance it to ``final_status``.

    Uses the same DAO path the production driver uses:
      - claim_send  →  pending
      - mark_sent   →  sent  (for sent/delivered/read/failed via webhook)
      - apply_broadcast_status → delivered | read | failed
    """
    broadcast_dao.claim_send(
        session,
        campaign=_CAMPAIGN,
        wa_digits=wa_digits,
        tier=tier,
        template_name="munshi_welcome_video_v1",
        language="en",
    )
    wamid = f"wamid.{wa_digits}"

    if final_status == "sent":
        broadcast_dao.mark_sent(session, campaign=_CAMPAIGN, wa_digits=wa_digits, wamid=wamid)
    elif final_status in ("delivered", "read"):
        broadcast_dao.mark_sent(session, campaign=_CAMPAIGN, wa_digits=wa_digits, wamid=wamid)
        broadcast_dao.apply_broadcast_status(
            session, wamid=wamid, status=final_status
        )
    elif final_status == "failed":
        broadcast_dao.mark_sent(session, campaign=_CAMPAIGN, wa_digits=wa_digits, wamid=wamid)
        broadcast_dao.apply_broadcast_status(
            session,
            wamid=wamid,
            status="failed",
            error_code=error_code,
            failure_reason="test failure",
        )

    session.flush()


# ---------------------------------------------------------------------------
# Fixture: a mixed-status ledger
#
# sent        : 1 row   (91001)
# delivered   : 2 rows  (91002, 91003)
# read        : 1 row   (91004)
# failed/131026: 2 rows (91005, 91006)
# failed/131049: 1 row  (91007)
# failed/other : 1 row  (91008)  — error_code=131000
#
# Total rows = 8 ; attempted = 8 ; pending = 0
# attempted = 1 + 2 + 1 + 4 = 8
# delivered_pct = 2/8 = 0.25
# read_pct = 1/8 = 0.125
# undeliverable = 2
# marketing_capped = 1
# other_failed = 1
# ---------------------------------------------------------------------------


@pytest.fixture()
def mixed_session(db_session):
    """Seed a mixed-status broadcast ledger and return the session."""
    _seed(db_session, wa_digits="91001", final_status="sent")
    _seed(db_session, wa_digits="91002", final_status="delivered")
    _seed(db_session, wa_digits="91003", final_status="delivered")
    _seed(db_session, wa_digits="91004", final_status="read")
    _seed(db_session, wa_digits="91005", final_status="failed", error_code=131026)
    _seed(db_session, wa_digits="91006", final_status="failed", error_code=131026)
    _seed(db_session, wa_digits="91007", final_status="failed", error_code=131049)
    _seed(db_session, wa_digits="91008", final_status="failed", error_code=131000)
    return db_session


# ---------------------------------------------------------------------------
# Count tests
# ---------------------------------------------------------------------------


def test_summarize_attempted(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    # 1 sent + 2 delivered + 1 read + 4 failed = 8
    assert s["attempted"] == 8


def test_summarize_sent(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    assert s["sent"] == 1


def test_summarize_delivered(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    assert s["delivered"] == 2


def test_summarize_read(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    assert s["read"] == 1


def test_summarize_failed(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    assert s["failed"] == 4


def test_summarize_total_rows(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    assert s["total_rows"] == 8


# ---------------------------------------------------------------------------
# Percentage tests
# ---------------------------------------------------------------------------


def test_summarize_delivered_pct(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    assert s["delivered_pct"] == pytest.approx(2 / 8)


def test_summarize_read_pct(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    assert s["read_pct"] == pytest.approx(1 / 8)


# ---------------------------------------------------------------------------
# Error-code breakdown tests
# ---------------------------------------------------------------------------


def test_summarize_undeliverable(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    assert s["undeliverable"] == 2  # 131026 × 2


def test_summarize_marketing_capped(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    assert s["marketing_capped"] == 1  # 131049 × 1


def test_summarize_other_failed(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    # 131000 failure only (not 131026, not 131049)
    assert s["other_failed"] == 1


def test_summarize_block_proxy_equals_other_failed(mixed_session):
    """block_proxy is documented as equal to other_failed (non-131026, non-131049 failures)."""
    s = summarize(mixed_session, _CAMPAIGN)
    assert s["block_proxy"] == s["other_failed"]


# ---------------------------------------------------------------------------
# Divide-by-zero guard: empty campaign
# ---------------------------------------------------------------------------


def test_summarize_empty_campaign_no_exception(db_session):
    """An empty campaign must return 0.0 for percentages without raising."""
    s = summarize(db_session, "campaign_does_not_exist")
    assert s["attempted"] == 0
    assert s["delivered_pct"] == 0.0
    assert s["read_pct"] == 0.0
    assert s["total_rows"] == 0
    assert s["failed"] == 0
    assert s["undeliverable"] == 0


def test_summarize_empty_campaign_pct_is_float(db_session):
    """Percentages for an empty campaign must be float (not int 0)."""
    s = summarize(db_session, "empty_campaign")
    assert isinstance(s["delivered_pct"], float)
    assert isinstance(s["read_pct"], float)


# ---------------------------------------------------------------------------
# Tier filter tests
# ---------------------------------------------------------------------------


def test_summarize_tier_filter_isolates_tier(db_session):
    """When tier= is provided, only rows with that tier are counted."""
    # Seed T1 rows
    _seed(db_session, wa_digits="91001", final_status="delivered", tier="T1")
    _seed(db_session, wa_digits="91002", final_status="delivered", tier="T1")
    # Seed T2 row
    _seed(db_session, wa_digits="91003", final_status="sent", tier="T2")

    t1 = summarize(db_session, _CAMPAIGN, tier="T1")
    t2 = summarize(db_session, _CAMPAIGN, tier="T2")
    all_ = summarize(db_session, _CAMPAIGN)

    assert t1["delivered"] == 2
    assert t1["total_rows"] == 2
    assert t2["delivered"] == 0
    assert t2["sent"] == 1
    assert t2["total_rows"] == 1
    assert all_["total_rows"] == 3


def test_summarize_tier_none_counts_all(db_session):
    """tier=None (default) must count rows across all tiers."""
    _seed(db_session, wa_digits="91001", final_status="sent", tier="T1")
    _seed(db_session, wa_digits="91002", final_status="sent", tier="T2")
    _seed(db_session, wa_digits="91003", final_status="sent", tier="T3")

    s = summarize(db_session, _CAMPAIGN)
    assert s["total_rows"] == 3
    assert s["sent"] == 3


# ---------------------------------------------------------------------------
# Metadata echo tests
# ---------------------------------------------------------------------------


def test_summarize_echoes_campaign(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    assert s["campaign"] == _CAMPAIGN


def test_summarize_echoes_tier_none(mixed_session):
    s = summarize(mixed_session, _CAMPAIGN)
    assert s["tier"] is None


def test_summarize_echoes_tier_when_filtered(db_session):
    s = summarize(db_session, _CAMPAIGN, tier="T1")
    assert s["tier"] == "T1"


# ---------------------------------------------------------------------------
# M3 — other_failed must count locally-failed rows with NULL error_code
# ---------------------------------------------------------------------------


def test_other_failed_counts_null_error_code(db_session):
    """M3 regression: a failed row with error_code=None must appear in other_failed.

    SQL NULL != N evaluates to NULL (not TRUE), so without an explicit
    OR error_code IS NULL the row was silently excluded from other_failed.
    """
    # Seed one failed row with error_code=None (local failure, no Meta code)
    _seed(db_session, wa_digits="92001", final_status="failed", error_code=None)
    # Also seed a row with 131026 so we confirm it does NOT count in other_failed
    _seed(db_session, wa_digits="92002", final_status="failed", error_code=131026)

    s = summarize(db_session, _CAMPAIGN)
    assert s["failed"] == 2
    assert s["undeliverable"] == 1        # 131026
    assert s["other_failed"] == 1, (
        f"other_failed should be 1 (NULL error_code row), got {s['other_failed']}. "
        "SQL NULL != 131026 is NULL not TRUE — must use OR error_code IS NULL."
    )
    assert s["block_proxy"] == s["other_failed"]


def test_other_failed_mixed_null_and_known_code(db_session):
    """NULL-code failures and other-code failures both count in other_failed."""
    _seed(db_session, wa_digits="93001", final_status="failed", error_code=None)
    _seed(db_session, wa_digits="93002", final_status="failed", error_code=None)
    _seed(db_session, wa_digits="93003", final_status="failed", error_code=131000)
    _seed(db_session, wa_digits="93004", final_status="failed", error_code=131026)
    _seed(db_session, wa_digits="93005", final_status="failed", error_code=131049)

    s = summarize(db_session, _CAMPAIGN)
    # 2 NULL + 1 × 131000 = 3 other_failed
    # 1 × 131026 = undeliverable, 1 × 131049 = marketing_capped
    assert s["undeliverable"] == 1
    assert s["marketing_capped"] == 1
    assert s["other_failed"] == 3
