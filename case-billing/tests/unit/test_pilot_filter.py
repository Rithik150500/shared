"""Sub-project C Phase 4: pilot filter + observability metric counters.

Tests the env-var-driven allowlist that gates upsell sends during the
7-day pilot launch window (spec section 2.8 pilot launch protocol).
"""
from __future__ import annotations

import os
import uuid

import pytest

from case_billing.nowlez.upsell import (
    PILOT_USER_IDS_ENV,
    UPSELL_CRON_DISABLED_ENV,
    get_metric,
    get_pilot_user_ids,
    is_cron_disabled,
    is_user_in_pilot,
    record_metric,
    reset_metrics,
)


@pytest.fixture(autouse=True)
def _clear_env_and_metrics(monkeypatch):
    """Ensure each test runs with a clean env + counter state."""
    monkeypatch.delenv(PILOT_USER_IDS_ENV, raising=False)
    monkeypatch.delenv(UPSELL_CRON_DISABLED_ENV, raising=False)
    reset_metrics()
    yield
    reset_metrics()


# ----------------------------------------------------------------------
# get_pilot_user_ids
# ----------------------------------------------------------------------


def test_pilot_filter_off_when_env_unset():
    """No env var → returns None → full rollout."""
    assert get_pilot_user_ids() is None


def test_pilot_filter_off_when_env_empty(monkeypatch):
    """Empty string env var → still None → full rollout."""
    monkeypatch.setenv(PILOT_USER_IDS_ENV, "")
    assert get_pilot_user_ids() is None


def test_pilot_filter_off_when_env_whitespace(monkeypatch):
    """Whitespace-only env var → None → full rollout."""
    monkeypatch.setenv(PILOT_USER_IDS_ENV, "   \t  ")
    assert get_pilot_user_ids() is None


def test_pilot_filter_parses_single_uuid(monkeypatch):
    """One UUID → set with that UUID."""
    u = uuid.uuid4()
    monkeypatch.setenv(PILOT_USER_IDS_ENV, str(u))
    assert get_pilot_user_ids() == {u}


def test_pilot_filter_parses_multiple_uuids(monkeypatch):
    """Comma-separated UUIDs → set with all of them."""
    u1, u2, u3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    monkeypatch.setenv(PILOT_USER_IDS_ENV, f"{u1},{u2},{u3}")
    assert get_pilot_user_ids() == {u1, u2, u3}


def test_pilot_filter_tolerates_spaces(monkeypatch):
    """Whitespace around commas + uuids is tolerated."""
    u1, u2 = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setenv(PILOT_USER_IDS_ENV, f" {u1} , {u2} ")
    assert get_pilot_user_ids() == {u1, u2}


def test_pilot_filter_skips_malformed_uuids(monkeypatch):
    """One malformed entry doesn't kill the whole list; counter incremented."""
    u = uuid.uuid4()
    monkeypatch.setenv(PILOT_USER_IDS_ENV, f"{u},not-a-uuid,also-bad")
    result = get_pilot_user_ids()
    assert result == {u}
    assert get_metric("pilot_filter_malformed") == 2


# ----------------------------------------------------------------------
# C-5: malformed-UUID observability + all-malformed hard-fail
# ----------------------------------------------------------------------


def test_pilot_filter_logs_warning_for_each_malformed_entry(monkeypatch, caplog):
    """C-5: pre-fix malformed entries were dropped silently. Now each one
    emits a WARNING line so operators see the typo without a dashboard
    round-trip.
    """
    import logging
    u = uuid.uuid4()
    monkeypatch.setenv(
        PILOT_USER_IDS_ENV,
        f"{u},not-a-uuid,short",
    )
    caplog.set_level(logging.WARNING, logger="case_billing.nowlez.upsell")
    result = get_pilot_user_ids()
    assert result == {u}
    warnings = [
        r for r in caplog.records
        if "Skipping malformed pilot UUID entry" in r.getMessage()
    ]
    assert len(warnings) == 2, (
        f"expected one WARNING per malformed entry; got {len(warnings)}: "
        f"{[r.getMessage() for r in warnings]}"
    )
    # Both bad entries should appear by their literal value.
    msgs = " ".join(r.getMessage() for r in warnings)
    assert "not-a-uuid" in msgs
    assert "short" in msgs


def test_pilot_filter_all_malformed_raises_runtime_error(monkeypatch):
    """C-5: if every entry is malformed the cron would otherwise silently
    process zero users. Hard-fail so the operator notices on the first run.
    """
    monkeypatch.setenv(
        PILOT_USER_IDS_ENV,
        "not-a-uuid,also-bad,half-a-uuid",
    )
    with pytest.raises(RuntimeError, match="every entry is malformed"):
        get_pilot_user_ids()


def test_pilot_filter_empty_env_var_still_returns_none(monkeypatch):
    """C-5 regression guard: the empty-env-var path (full rollout) must
    NOT be affected by the new all-malformed RuntimeError gate.
    """
    monkeypatch.setenv(PILOT_USER_IDS_ENV, "")
    assert get_pilot_user_ids() is None
    monkeypatch.setenv(PILOT_USER_IDS_ENV, "   ,  ,  ")
    # Whitespace-only entries are skipped (not counted as malformed) so
    # this returns an empty set, not RuntimeError. Per C-5 we differentiate
    # "empty after parse" (no real entries) from "every entry was malformed"
    # — only the latter is operator error worth crashing on.
    assert get_pilot_user_ids() == set()


# ----------------------------------------------------------------------
# is_user_in_pilot
# ----------------------------------------------------------------------


def test_user_in_pilot_true_when_filter_off():
    """All users pass when filter is unset (full rollout)."""
    assert is_user_in_pilot(uuid.uuid4()) is True


def test_user_in_pilot_true_when_user_in_allowlist(monkeypatch):
    """User on allowlist → True."""
    u = uuid.uuid4()
    monkeypatch.setenv(PILOT_USER_IDS_ENV, str(u))
    assert is_user_in_pilot(u) is True


def test_user_in_pilot_false_when_user_not_in_allowlist(monkeypatch):
    """User not on allowlist → False (silently skipped by cron)."""
    pilot_user = uuid.uuid4()
    other_user = uuid.uuid4()
    monkeypatch.setenv(PILOT_USER_IDS_ENV, str(pilot_user))
    assert is_user_in_pilot(other_user) is False


# ----------------------------------------------------------------------
# is_cron_disabled (kill switch)
# ----------------------------------------------------------------------


def test_cron_disabled_default_false():
    """Unset env var → cron runs normally."""
    assert is_cron_disabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "True", "yes", "Yes", "on", "ON"])
def test_cron_disabled_true_for_truthy_values(monkeypatch, val):
    """Common truthy strings flip the kill switch."""
    monkeypatch.setenv(UPSELL_CRON_DISABLED_ENV, val)
    assert is_cron_disabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "  ", "anything-else"])
def test_cron_disabled_false_for_falsy_values(monkeypatch, val):
    """Non-truthy strings leave cron running."""
    monkeypatch.setenv(UPSELL_CRON_DISABLED_ENV, val)
    assert is_cron_disabled() is False


# ----------------------------------------------------------------------
# record_metric / get_metric / reset_metrics
# ----------------------------------------------------------------------


def test_record_metric_increments_counter():
    """Counter starts at 0 and increments by 1 each call."""
    assert get_metric("upsell_sent_total") == 0
    record_metric("upsell_sent_total")
    assert get_metric("upsell_sent_total") == 1
    record_metric("upsell_sent_total")
    assert get_metric("upsell_sent_total") == 2


def test_record_metric_custom_value():
    """Caller can pass an explicit increment."""
    record_metric("upsell_eligible_users_gauge", 47)
    assert get_metric("upsell_eligible_users_gauge") == 47


def test_reset_metrics_zeros_all_counters():
    """reset_metrics clears every counter (test isolation)."""
    record_metric("a", 10)
    record_metric("b", 20)
    reset_metrics()
    assert get_metric("a") == 0
    assert get_metric("b") == 0


def test_metrics_isolated_across_keys():
    """Different metric names don't bleed into each other."""
    record_metric("upsell_sent_total")
    record_metric("upsell_clicked_total", 5)
    assert get_metric("upsell_sent_total") == 1
    assert get_metric("upsell_clicked_total") == 5
