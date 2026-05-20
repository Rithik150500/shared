"""Munshi upsell cron logic.

Three stages: initial (80 cases OR ₹1000 spend), reminder (150 cases OR
₹1500 spend), final (195 cases — case-count only for final). Each stage
fires at most once per user (UNIQUE constraint in DB). 14-day cooling-off
between stages.

The eligibility query (Section 2.1 of the sub-project C spec) is run by
the daily cron; this module exposes the stage-determination logic
separately so it can be unit-tested without mocking the cron query, and
provides DAO helpers for recording sends + conversions.

Sub-project C Phase 4 (2026-05-20) adds the pilot-rollout filter
(SUBPROJECT_C_PILOT_USERS env var) + Prometheus metric counters.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session


log = logging.getLogger(__name__)


COOLING_OFF_DAYS = 14

# Thresholds (Section 2.2 of spec):
CASE_COUNT_INITIAL = 80
CASE_COUNT_REMINDER = 150
CASE_COUNT_FINAL = 195
SPEND_INITIAL_RUPEES = 1000
SPEND_REMINDER_RUPEES = 1500
# Final stage: case-count only (~195 cases = 97.5% of 200 cap)


# ----------------------------------------------------------------------
# Sub-project C Phase 4: pilot-rollout filter + observability
# ----------------------------------------------------------------------

# Comma-separated list of user_id UUIDs allowed to receive upsells during
# the pilot phase. When set, the daily cron silently skips every user not
# in this set. When unset OR empty, the cron processes all eligible users
# (full rollout).
#
# Pilot launch protocol (spec section 2.8): identify 10-20 Munshi users in
# 80-case range or ≥₹1,000 cumulative spend, populate this env var with
# their user_ids, monitor for 7 days, then unset for full rollout.
PILOT_USER_IDS_ENV = "SUBPROJECT_C_PILOT_USERS"

# Kill switch — if "true"/"1"/"yes", the upsell cron is a no-op. Used for
# instant rollback (section 7.4 rollback table: ~5 min).
UPSELL_CRON_DISABLED_ENV = "UPSELL_CRON_DISABLED"


def get_pilot_user_ids() -> set[uuid.UUID] | None:
    """Return the pilot allowlist, or None if pilot filter is OFF (full rollout).

    Parses comma-separated UUIDs from the SUBPROJECT_C_PILOT_USERS env var.
    Malformed UUIDs are dropped with a per-entry WARNING log so operators
    can spot a typo without taking down the pipeline.

    C-5 (audit fix): pre-fix the malformed entries were dropped silently
    (only a metric counter). An operator who typo'd one of the 10 pilot
    UUIDs would silently get a 9-user pilot instead. Now each malformed
    entry emits a WARNING line naming the bad value. **If every entry
    is malformed** (env var was non-empty but the resulting set is empty),
    we raise ``RuntimeError`` — silently processing zero users would be
    worse than the cron not running at all, and the operator wants to
    know immediately rather than 24 hours later when they check metrics.
    """
    raw = os.environ.get(PILOT_USER_IDS_ENV, "").strip()
    if not raw:
        return None
    ids: set[uuid.UUID] = set()
    malformed_count = 0
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            ids.add(uuid.UUID(entry))
        except ValueError:
            malformed_count += 1
            # C-5: log per-entry so the operator sees which value was
            # bad without a metrics dashboard round-trip. Length is
            # included because copy-paste truncation is the most common
            # cause of bad UUIDs in env vars.
            log.warning(
                "Skipping malformed pilot UUID entry: %r (length %d)",
                entry,
                len(entry),
            )
            _METRICS["pilot_filter_malformed"] = _METRICS.get("pilot_filter_malformed", 0) + 1
            continue
    # C-5: if the env var was non-empty but EVERY entry was malformed the
    # cron would otherwise silently process zero users. Hard-fail so the
    # operator notices on the first run.
    if malformed_count > 0 and not ids:
        raise RuntimeError(
            f"{PILOT_USER_IDS_ENV} is set but every entry is malformed "
            f"({malformed_count} bad UUID(s)). Fix the env var or unset "
            "it to disable the pilot filter."
        )
    return ids


def is_cron_disabled() -> bool:
    """Kill switch — read once per cron run."""
    val = (os.environ.get(UPSELL_CRON_DISABLED_ENV, "") or "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def is_user_in_pilot(user_id: uuid.UUID) -> bool:
    """True iff user is allowed by the current pilot filter.

    Returns True for ALL users when the filter is OFF (full rollout).
    """
    allowlist = get_pilot_user_ids()
    if allowlist is None:
        return True
    return user_id in allowlist


# Minimal in-process counters. Production swaps in a real Prometheus
# client; this in-process dict lets unit tests assert that the cron
# pipeline went through the expected branches. Per spec section 7.2 the
# metric names are stable contracts.
_METRICS: dict[str, int] = {}


def record_metric(name: str, value: int = 1) -> None:
    """Increment a named counter. Used by the cron for the spec's
    Prometheus metrics (upsell_sent_total, upsell_skipped_pilot_total,
    upsell_skipped_cooling_off_total, etc.)."""
    _METRICS[name] = _METRICS.get(name, 0) + value


def reset_metrics() -> None:
    """Test helper — zero the in-process counters."""
    _METRICS.clear()


def get_metric(name: str) -> int:
    """Test helper — read the current counter."""
    return _METRICS.get(name, 0)


@dataclass
class UpsellEligibility:
    """Cron-query result row, agnostic of DB driver."""

    user_id: uuid.UUID
    active_cases: int
    lifetime_spend_rupees: int
    last_sent_at: datetime | None


def determine_upsell_stage(
    elig: UpsellEligibility, sent_stages: set[str],
) -> str | None:
    """Pure function: given current eligibility + which stages were already
    sent, return the next stage to send or None.

    Returns ``"initial"``, ``"reminder"``, ``"final"``, or ``None``.
    Caller is responsible for the cooling-off check (which depends on
    last_sent_at being recent) — see :func:`is_within_cooling_off`.

    The order of checks descends from highest-priority stage so that a
    user who blows past the initial threshold without a send (e.g.,
    burst case-add) still gets the most-relevant stage rather than the
    earliest one. Each stage's eligibility is independent of the
    previous ones being sent.
    """
    if elig.active_cases >= CASE_COUNT_FINAL and "final" not in sent_stages:
        return "final"
    if (
        (
            elig.active_cases >= CASE_COUNT_REMINDER
            or elig.lifetime_spend_rupees >= SPEND_REMINDER_RUPEES
        )
        and "reminder" not in sent_stages
    ):
        return "reminder"
    if (
        (
            elig.active_cases >= CASE_COUNT_INITIAL
            or elig.lifetime_spend_rupees >= SPEND_INITIAL_RUPEES
        )
        and "initial" not in sent_stages
    ):
        return "initial"
    return None


def is_within_cooling_off(
    last_sent_at: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Returns True if last_sent_at is within COOLING_OFF_DAYS of now.

    Naive datetimes (no tzinfo) are assumed UTC for backward-compat with
    callers that strip tzinfo on the way out of the DB.
    """
    if last_sent_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    last_aware = (
        last_sent_at
        if last_sent_at.tzinfo
        else last_sent_at.replace(tzinfo=timezone.utc)
    )
    return last_aware > now - timedelta(days=COOLING_OFF_DAYS)


def was_stage_sent(
    session: Session, *, user_id: uuid.UUID, stage: str,
) -> bool:
    """Has the given upsell stage already been sent to this user?"""
    from data_access.models import MunshiUpsellEvent

    row = session.execute(
        select(MunshiUpsellEvent.id)
        .where(MunshiUpsellEvent.user_id == user_id)
        .where(MunshiUpsellEvent.stage == stage)
        .limit(1)
    ).first()
    return row is not None


def record_upsell_event(
    session: Session,
    *,
    user_id: uuid.UUID,
    stage: str,
    trigger_reason: str,
    case_count_at_send: int,
    spend_at_send_rupees: int,
    template_name: str,
    meta_message_id: str | None = None,
) -> None:
    """Insert a new munshi_upsell_events row. Caller commits.

    UNIQUE(user_id, stage) on the table guards against double-sends from
    a racing cron worker — if a duplicate is attempted the second
    transaction's commit() raises IntegrityError, which the cron treats
    as a benign "another worker beat me to it" no-op.
    """
    from data_access.models import MunshiUpsellEvent

    event = MunshiUpsellEvent(
        user_id=user_id,
        stage=stage,
        trigger_reason=trigger_reason,
        case_count_at_send=case_count_at_send,
        spend_at_send_rupees=spend_at_send_rupees,
        template_name=template_name,
        meta_message_id=meta_message_id,
    )
    session.add(event)


def record_upgrade_conversion(
    session: Session, *, user_id: uuid.UUID, tier: str,
) -> None:
    """Mark the most-recent unconverted upsell event as converted.

    Called from the Razorpay subscription.activated webhook handler. If
    the user has no unconverted upsell events (e.g., they upgraded via a
    different path — direct Nowlez signup, trial conversion), this is a
    no-op.

    Only the most-recent unconverted event is marked. Older un-acted-on
    upsells from previous threshold-cross events stay unconverted in the
    audit trail; the analytics layer joins on (most-recent before
    conversion) to attribute the conversion to its likely trigger.
    """
    from data_access.models import MunshiUpsellEvent

    event = session.execute(
        select(MunshiUpsellEvent)
        .where(MunshiUpsellEvent.user_id == user_id)
        .where(MunshiUpsellEvent.converted_at.is_(None))
        .order_by(MunshiUpsellEvent.sent_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if event is None:
        return
    event.converted_at = datetime.now(timezone.utc)
    event.converted_to_tier = tier


__all__ = [
    "COOLING_OFF_DAYS",
    "CASE_COUNT_INITIAL",
    "CASE_COUNT_REMINDER",
    "CASE_COUNT_FINAL",
    "SPEND_INITIAL_RUPEES",
    "SPEND_REMINDER_RUPEES",
    "UpsellEligibility",
    "determine_upsell_stage",
    "is_within_cooling_off",
    "was_stage_sent",
    "record_upsell_event",
    "record_upgrade_conversion",
]
