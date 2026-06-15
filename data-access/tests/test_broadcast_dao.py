"""TDD tests for the broadcast ledger + suppression DAO.

Uses the SQLite ``db_session`` fixture (in-memory) defined in conftest.py.
All tests are dialect-agnostic — the DAO's ON CONFLICT DO NOTHING branches
handle both Postgres and SQLite; here we exercise the SQLite path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from data_access.daos import broadcast_dao
from data_access.models import WaBroadcastLog, WaSuppression


# ---------------------------------------------------------------------------
# Suppression table tests
# ---------------------------------------------------------------------------


def test_suppress_marks_phone_suppressed(db_session):
    broadcast_dao.suppress(db_session, wa_digits="919643460175", reason="stop")
    assert broadcast_dao.is_suppressed(db_session, "919643460175") is True


def test_is_suppressed_miss_returns_false(db_session):
    assert broadcast_dao.is_suppressed(db_session, "919999999999") is False


def test_suppress_is_idempotent(db_session):
    """Calling suppress twice on the same number must not raise and must
    leave the number suppressed."""
    broadcast_dao.suppress(db_session, wa_digits="919643460175", reason="stop")
    # Second call — ON CONFLICT DO NOTHING must silently skip.
    broadcast_dao.suppress(db_session, wa_digits="919643460175", reason="manual")
    assert broadcast_dao.is_suppressed(db_session, "919643460175") is True
    # Only one row (first write wins).
    rows = db_session.query(WaSuppression).filter_by(wa_digits="919643460175").all()
    assert len(rows) == 1


def test_load_suppressed_set_returns_all_digits(db_session):
    broadcast_dao.suppress(db_session, wa_digits="91111", reason="stop")
    broadcast_dao.suppress(db_session, wa_digits="91222", reason="block")
    result = broadcast_dao.load_suppressed_set(db_session)
    assert "91111" in result
    assert "91222" in result


def test_load_suppressed_set_empty(db_session):
    assert broadcast_dao.load_suppressed_set(db_session) == set()


# ---------------------------------------------------------------------------
# claim_send: exactly-once ledger
# ---------------------------------------------------------------------------


def test_claim_send_first_returns_true(db_session):
    claimed = broadcast_dao.claim_send(
        db_session,
        campaign="launch_2026_06",
        wa_digits="919643460175",
        tier="tier_1",
        template_name="munshi_broadcast_v1",
        language="en",
    )
    assert claimed is True


def test_claim_send_second_identical_returns_false(db_session):
    kwargs = dict(
        campaign="launch_2026_06",
        wa_digits="919643460175",
        tier="tier_1",
        template_name="munshi_broadcast_v1",
        language="en",
    )
    assert broadcast_dao.claim_send(db_session, **kwargs) is True
    # Second identical claim — must not raise, returns False.
    assert broadcast_dao.claim_send(db_session, **kwargs) is False


def test_claim_send_different_campaign_is_independent(db_session):
    kwargs = dict(
        wa_digits="919643460175",
        tier="tier_1",
        template_name="munshi_broadcast_v1",
        language="en",
    )
    assert broadcast_dao.claim_send(db_session, campaign="camp_A", **kwargs) is True
    assert broadcast_dao.claim_send(db_session, campaign="camp_B", **kwargs) is True


# ---------------------------------------------------------------------------
# mark_sent + apply_broadcast_status (status flow)
# ---------------------------------------------------------------------------


def test_mark_sent_then_apply_failed_status(db_session):
    broadcast_dao.claim_send(
        db_session,
        campaign="launch_2026_06",
        wa_digits="919643460175",
        tier="tier_1",
        template_name="munshi_broadcast_v1",
        language="en",
    )
    broadcast_dao.mark_sent(
        db_session,
        campaign="launch_2026_06",
        wa_digits="919643460175",
        wamid="wamid.abc123",
    )

    # Now simulate a failed status update via apply_broadcast_status.
    n = broadcast_dao.apply_broadcast_status(
        db_session,
        wamid="wamid.abc123",
        status="failed",
        error_code=131026,
        failure_reason="Re-engagement window",
    )
    assert n == 1

    row = broadcast_dao.get_by_wamid(db_session, "wamid.abc123")
    assert row is not None
    assert row.status == "failed"
    assert row.error_code == 131026
    assert row.failure_reason == "Re-engagement window"
    assert row.failed_at is not None


def test_apply_broadcast_status_delivered_sets_timestamp(db_session):
    broadcast_dao.claim_send(
        db_session,
        campaign="camp",
        wa_digits="919111111111",
        tier=None,
        template_name="t",
        language="en",
    )
    broadcast_dao.mark_sent(
        db_session,
        campaign="camp",
        wa_digits="919111111111",
        wamid="wamid.del1",
    )
    n = broadcast_dao.apply_broadcast_status(
        db_session,
        wamid="wamid.del1",
        status="delivered",
    )
    assert n == 1
    row = broadcast_dao.get_by_wamid(db_session, "wamid.del1")
    assert row.status == "delivered"
    assert row.delivered_at is not None
    assert row.failed_at is None


def test_apply_broadcast_status_unknown_wamid_returns_zero(db_session):
    n = broadcast_dao.apply_broadcast_status(
        db_session, wamid="wamid.doesnotexist", status="delivered"
    )
    assert n == 0


# ---------------------------------------------------------------------------
# already_done_set
# ---------------------------------------------------------------------------


def test_already_done_set_contains_claimed_digits(db_session):
    broadcast_dao.claim_send(
        db_session,
        campaign="c1",
        wa_digits="91aaa",
        tier="tier_1",
        template_name="t",
        language="en",
    )
    broadcast_dao.claim_send(
        db_session,
        campaign="c1",
        wa_digits="91bbb",
        tier="tier_1",
        template_name="t",
        language="en",
    )
    done = broadcast_dao.already_done_set(db_session, "c1")
    assert "91aaa" in done
    assert "91bbb" in done


def test_already_done_set_scoped_to_campaign(db_session):
    broadcast_dao.claim_send(
        db_session,
        campaign="camp_X",
        wa_digits="91xxx",
        tier=None,
        template_name="t",
        language="en",
    )
    done = broadcast_dao.already_done_set(db_session, "camp_Y")
    assert "91xxx" not in done


# ---------------------------------------------------------------------------
# sent_count_since
# ---------------------------------------------------------------------------


def test_sent_count_since_counts_sent_rows_in_window(db_session):
    # Claim + mark sent two rows.
    for digits in ("91001", "91002"):
        broadcast_dao.claim_send(
            db_session,
            campaign="daily",
            wa_digits=digits,
            tier="tier_1",
            template_name="t",
            language="en",
        )
        broadcast_dao.mark_sent(
            db_session, campaign="daily", wa_digits=digits, wamid=f"wamid.{digits}"
        )

    # Claim but don't mark sent a third row (status=pending).
    broadcast_dao.claim_send(
        db_session,
        campaign="daily",
        wa_digits="91003",
        tier="tier_1",
        template_name="t",
        language="en",
    )

    count = broadcast_dao.sent_count_since(db_session, "daily", hours=24)
    assert count == 2


def test_sent_count_since_excludes_outside_window(db_session):
    broadcast_dao.claim_send(
        db_session,
        campaign="daily",
        wa_digits="91001",
        tier=None,
        template_name="t",
        language="en",
    )
    broadcast_dao.mark_sent(
        db_session, campaign="daily", wa_digits="91001", wamid="wamid.old"
    )
    # Manually backdate the sent_at so it falls outside the 1-hour window.
    row = broadcast_dao.get_by_wamid(db_session, "wamid.old")
    row.sent_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db_session.flush()

    count = broadcast_dao.sent_count_since(db_session, "daily", hours=1)
    assert count == 0


def test_sent_count_since_scoped_to_campaign(db_session):
    broadcast_dao.claim_send(
        db_session,
        campaign="camp_A",
        wa_digits="91001",
        tier=None,
        template_name="t",
        language="en",
    )
    broadcast_dao.mark_sent(
        db_session, campaign="camp_A", wa_digits="91001", wamid="wamid.a"
    )
    count = broadcast_dao.sent_count_since(db_session, "camp_B", hours=24)
    assert count == 0
