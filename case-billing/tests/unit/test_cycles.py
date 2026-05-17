"""Anniversary cycle-math unit tests (Task 6.1.2).

`compute_cycle_window` returns the (start, end) instants in `Asia/Kolkata`
of the billing cycle that ends today, where "today" is the IST calendar
day passed in. Anniversaries on Jan-29, 30 or 31 are clamped to the last
day of any short month — Feb 28 in a non-leap year, Feb 29 in a leap
year, Apr 30 etc. The clamping is local to a given cycle: the next cycle
that lands on a long month restores the original anniversary day.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from case_billing.munshi.cycles import (
    IST,
    compute_cycle_window,
    next_anniversary_date,
)


# --- TZ contract -----------------------------------------------------------


def test_ist_is_asia_kolkata() -> None:
    """The module's IST constant is `Asia/Kolkata` (no DST in India)."""
    assert IST.key == "Asia/Kolkata"


# --- compute_cycle_window — happy path -------------------------------------


@pytest.mark.parametrize(
    "anniversary,today,expected_start,expected_end",
    [
        # Mid-month anniversary, one full cycle later.
        (
            date(2026, 3, 15),
            date(2026, 4, 15),
            date(2026, 3, 15),
            date(2026, 4, 15),
        ),
        # Same anniversary, today *is* the anniversary again the next year.
        (
            date(2025, 6, 1),
            date(2026, 6, 1),
            date(2026, 5, 1),
            date(2026, 6, 1),
        ),
    ],
)
def test_compute_cycle_window_basic(
    anniversary: date,
    today: date,
    expected_start: date,
    expected_end: date,
) -> None:
    start, end = compute_cycle_window(anniversary, today)
    assert start.tzinfo is not None
    assert end.tzinfo is not None
    assert start == datetime(
        expected_start.year, expected_start.month, expected_start.day,
        tzinfo=IST,
    )
    assert end == datetime(
        expected_end.year, expected_end.month, expected_end.day,
        tzinfo=IST,
    )


# --- compute_cycle_window — Feb 28 / 29 clamp ------------------------------


def test_compute_cycle_window_jan31_to_feb28_non_leap() -> None:
    """Jan-31 anniversary, today Feb-28 non-leap year → clamp to Feb 28."""
    start, end = compute_cycle_window(
        anniversary_date=date(2026, 1, 31),
        today=date(2026, 2, 28),
    )
    # previous anniversary day in Feb (non-leap) clamps from 31 to 28.
    assert start == datetime(2026, 1, 31, tzinfo=IST)
    assert end == datetime(2026, 2, 28, tzinfo=IST)


def test_compute_cycle_window_jan31_to_mar01_clamp_boundary() -> None:
    """Cycle that just crossed into March: prev anniversary is Feb-28 (clamped from 31)."""
    start, end = compute_cycle_window(
        anniversary_date=date(2026, 1, 31),
        today=date(2026, 3, 1),
    )
    assert start == datetime(2026, 2, 28, tzinfo=IST)
    assert end == datetime(2026, 3, 1, tzinfo=IST)


def test_compute_cycle_window_leap_year_feb29() -> None:
    """Anniversary 2024-02-29 (leap day), today 2025-02-28 → clamp to Feb 28."""
    start, end = compute_cycle_window(
        anniversary_date=date(2024, 2, 29),
        today=date(2025, 2, 28),
    )
    # Anniversary on 29; Feb 2025 only has 28 days → clamped.
    assert start == datetime(2025, 1, 29, tzinfo=IST)
    assert end == datetime(2025, 2, 28, tzinfo=IST)


def test_compute_cycle_window_leap_year_feb29_to_feb29() -> None:
    """Leap-day anniversary in a leap year: Feb 29 lands exactly."""
    start, end = compute_cycle_window(
        anniversary_date=date(2024, 2, 29),
        today=date(2024, 2, 29),
    )
    assert start == datetime(2024, 1, 29, tzinfo=IST)
    assert end == datetime(2024, 2, 29, tzinfo=IST)


def test_compute_cycle_window_anniversary_30_in_april() -> None:
    """Day-30 anniversary, today Apr-30 → Apr has 30 days, no clamp."""
    start, end = compute_cycle_window(
        anniversary_date=date(2026, 1, 30),
        today=date(2026, 4, 30),
    )
    assert start == datetime(2026, 3, 30, tzinfo=IST)
    assert end == datetime(2026, 4, 30, tzinfo=IST)


# --- compute_cycle_window — DST sanity -------------------------------------


def test_compute_cycle_window_no_dst_offset_changes_within_window() -> None:
    """IST has no DST so utcoffset is +05:30 at both endpoints."""
    start, end = compute_cycle_window(
        anniversary_date=date(2026, 3, 1),
        today=date(2026, 4, 1),
    )
    assert start.utcoffset() == end.utcoffset()
    # Sanity check the constant value.
    assert str(start.utcoffset()) == "5:30:00"


# --- next_anniversary_date ------------------------------------------------


@pytest.mark.parametrize(
    "anniversary,today,expected",
    [
        # Anniversary is monthly (day 15), today Jan 1 → next Jan 15.
        (date(2020, 3, 15), date(2026, 1, 1), date(2026, 1, 15)),
        # Today on the anniversary day → next month's anniversary (one cycle out).
        (date(2020, 3, 15), date(2026, 3, 15), date(2026, 4, 15)),
        # Today after this month's anniversary → next month.
        (date(2020, 3, 15), date(2026, 4, 1), date(2026, 4, 15)),
    ],
)
def test_next_anniversary_date_basic(
    anniversary: date, today: date, expected: date,
) -> None:
    assert next_anniversary_date(anniversary, today) == expected


def test_next_anniversary_date_clamps_to_feb_28_non_leap() -> None:
    """Anniversary day 31, today Feb-1 non-leap → next Feb 28 (clamp)."""
    nxt = next_anniversary_date(
        anniversary=date(2025, 1, 31),
        today=date(2026, 2, 1),
    )
    # Feb non-leap year 2026 → max day 28.
    assert nxt == date(2026, 2, 28)


def test_next_anniversary_date_feb29_in_non_leap_year() -> None:
    """Feb-29 anniversary, today Feb 1 in non-leap year → clamp to Feb 28."""
    nxt = next_anniversary_date(
        anniversary=date(2024, 2, 29),
        today=date(2025, 2, 1),
    )
    assert nxt == date(2025, 2, 28)


def test_next_anniversary_date_31_restored_in_long_month() -> None:
    """Day-31 anniversary clamps to Feb 28 then restores in March."""
    # Today is March 1 — next anniversary is March 31 (restored).
    nxt = next_anniversary_date(
        anniversary=date(2025, 1, 31),
        today=date(2026, 3, 1),
    )
    assert nxt == date(2026, 3, 31)
