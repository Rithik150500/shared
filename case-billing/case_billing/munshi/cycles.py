"""Anniversary-based Munshi billing cycle math (spec Section 1.2).

Every paid Munshi user picks a billing anniversary day-of-month at sign-up
(captured in ``users_munshi.billing_anniversary_date``). Each month, the
postpaid invoice covers the window *(previous anniversary, today]*; there
is no proration.

Two operations live here, both deterministic and timezone-aware:

* :func:`compute_cycle_window` — returns ``(start, end)`` instants in
  ``Asia/Kolkata`` describing the billing period ending today. Used at
  invoice generation time.
* :func:`next_anniversary_date` — returns the next calendar date the user
  will be billed. Used by the cron scheduler to decide who to invoice.

Calendar edge cases are handled by clamping any day-of-month greater than
the target month's max length to the last day of that month. So a Jan-31
anniversary lands on Feb-28 in 2026 (non-leap) and Feb-29 in 2024, while
Feb-29 anniversaries clamp to Feb-28 in non-leap years. Crucially the
*original* anniversary day is remembered: the next cycle that lands on a
long month restores the full date (a Jan-31 anniversary still bills on
Mar-31 even after passing through a clamped Feb cycle).

IST has no daylight-saving offset, so the returned datetimes always carry
``utcoffset() == +05:30`` regardless of the cycle endpoints.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime
from zoneinfo import ZoneInfo

IST: ZoneInfo = ZoneInfo("Asia/Kolkata")


def _clamp_day_to_month(year: int, month: int, day: int) -> date:
    """Return ``date(year, month, min(day, last_day_of_month))``.

    Used so a day-of-month above the month's max length (e.g. day=31 in
    February) snaps to the month's last calendar day. Months that have
    the requested day are returned verbatim.
    """
    max_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, max_day))


def _previous_anniversary_calendar_day(
    anniversary_day: int, today: date,
) -> date:
    """Return the most recent calendar date < today that matches the anniversary day.

    Steps backward through months: if today's month has the (clamped)
    anniversary day and that day is strictly before today, that's the
    previous anniversary; otherwise reach back to the prior month.
    """
    # Try this month first.
    candidate = _clamp_day_to_month(today.year, today.month, anniversary_day)
    if candidate < today:
        return candidate
    # Today is on-or-before this month's anniversary date — step back a month.
    if today.month == 1:
        prev_year, prev_month = today.year - 1, 12
    else:
        prev_year, prev_month = today.year, today.month - 1
    return _clamp_day_to_month(prev_year, prev_month, anniversary_day)


def compute_cycle_window(
    anniversary_date: date,
    today: date,
) -> tuple[datetime, datetime]:
    """Return the (start, end) of the billing cycle that ends on ``today``.

    The cycle is *closed* at the start (the previous anniversary day) and
    *also* closed at the end (today's anniversary day) — the SQL DAO uses
    ``[)`` semantics on its own, so the half-open vs. closed distinction
    is irrelevant to the caller; we just hand back the two endpoints.

    Args:
        anniversary_date: The user's billing anniversary as a calendar
            date. Only the day-of-month is used to compute the cycle
            endpoints; the year and month are ignored.
        today: The date the invoice is being generated for, expressed in
            the user's local (IST) calendar.

    Returns:
        ``(cycle_start_ist, cycle_end_ist)`` — both ``datetime`` objects
        with ``tzinfo=Asia/Kolkata`` and time-component 00:00:00.

    Examples:
        Mid-month anniversary (no clamp):

        >>> from datetime import date
        >>> start, end = compute_cycle_window(date(2026, 3, 15), date(2026, 4, 15))
        >>> start.date(), end.date()
        (datetime.date(2026, 3, 15), datetime.date(2026, 4, 15))

        Jan-31 anniversary in a non-leap February clamps to Feb 28:

        >>> start, end = compute_cycle_window(date(2026, 1, 31), date(2026, 2, 28))
        >>> end.date()
        datetime.date(2026, 2, 28)
    """
    anniversary_day = anniversary_date.day
    prev = _previous_anniversary_calendar_day(anniversary_day, today)
    cycle_start = datetime(prev.year, prev.month, prev.day, tzinfo=IST)
    cycle_end = datetime(today.year, today.month, today.day, tzinfo=IST)
    return cycle_start, cycle_end


def next_anniversary_date(anniversary: date, today: date) -> date:
    """Return the calendar date of the next anniversary strictly after ``today``.

    Behaviour notes:

    * "Strictly after" — if ``today`` *is* the anniversary day, the
      returned date is one cycle (one month) later, not today. This
      matches the cron's contract: once we've invoiced for today's cycle,
      the next anniversary is the next month's.
    * Day-of-month is clamped to the target month's last calendar day if
      necessary, but the original anniversary day is remembered for
      subsequent months — a Jan-31 anniversary clamps to Feb 28 then
      restores to Mar 31.
    """
    anniversary_day = anniversary.day
    # Walk forward month-by-month until we find a date > today.
    year, month = today.year, today.month
    for _ in range(14):  # at most ~13 iterations to step ≤1 year ahead
        candidate = _clamp_day_to_month(year, month, anniversary_day)
        if candidate > today:
            return candidate
        # Advance one month.
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    # Loop guard — unreachable in practice (a year of months covers any
    # anniversary day) but keeps mypy happy.
    raise RuntimeError("Failed to locate next anniversary within 14 months")
