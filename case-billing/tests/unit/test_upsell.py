"""Phase 1: upsell stage determination + cooling-off."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from case_billing.nowlez.upsell import (
    COOLING_OFF_DAYS,
    UpsellEligibility,
    determine_upsell_stage,
    is_within_cooling_off,
)


def _elig(
    active_cases: int,
    spend: int,
    *,
    last_sent_at: datetime | None = None,
) -> UpsellEligibility:
    return UpsellEligibility(
        user_id=uuid.uuid4(),
        active_cases=active_cases,
        lifetime_spend_rupees=spend,
        last_sent_at=last_sent_at,
    )


# --- determine_upsell_stage --------------------------------------------------


def test_no_stage_below_initial_threshold():
    assert determine_upsell_stage(_elig(50, 500), set()) is None


def test_initial_fires_at_80_cases():
    assert determine_upsell_stage(_elig(80, 0), set()) == "initial"


def test_initial_fires_at_1000_spend_even_with_few_cases():
    assert determine_upsell_stage(_elig(10, 1000), set()) == "initial"


def test_reminder_fires_at_150_cases():
    assert determine_upsell_stage(_elig(150, 0), {"initial"}) == "reminder"


def test_reminder_fires_at_1500_spend():
    assert determine_upsell_stage(_elig(50, 1500), {"initial"}) == "reminder"


def test_final_fires_at_195_cases_only():
    # Spend alone doesn't trigger final.
    assert determine_upsell_stage(
        _elig(50, 10000), {"initial", "reminder"},
    ) is None
    assert determine_upsell_stage(
        _elig(195, 0), {"initial", "reminder"},
    ) == "final"


def test_returns_none_if_stage_already_sent():
    assert determine_upsell_stage(_elig(80, 0), {"initial"}) is None
    assert determine_upsell_stage(
        _elig(150, 0), {"initial", "reminder"},
    ) is None
    assert determine_upsell_stage(
        _elig(195, 0), {"initial", "reminder", "final"},
    ) is None


def test_skips_initial_for_user_above_reminder_threshold():
    # User hit reminder threshold without ever getting initial sent —
    # fires reminder directly (highest-priority unmet stage wins).
    assert determine_upsell_stage(_elig(150, 0), set()) == "reminder"


# --- is_within_cooling_off ---------------------------------------------------


def test_cooling_off_recent_send_returns_true():
    recent = datetime.now(timezone.utc) - timedelta(days=COOLING_OFF_DAYS - 1)
    assert is_within_cooling_off(recent) is True


def test_cooling_off_old_send_returns_false():
    old = datetime.now(timezone.utc) - timedelta(days=COOLING_OFF_DAYS + 1)
    assert is_within_cooling_off(old) is False


def test_cooling_off_none_returns_false():
    assert is_within_cooling_off(None) is False
