"""WABA account-level webhook event parsing and alerting.

Meta sends account-level events under ``entry[].changes[]`` with a ``field``
value distinct from the usual ``"messages"`` / ``"statuses"`` fields.  This
module handles three such fields:

``phone_number_quality_update``
    Quality rating change for the linked phone number (FLAGGED, ONLINED,
    UPGRADE, DOWNGRADE, etc.).

``message_template_status_update``
    Template lifecycle events: APPROVED, REJECTED, PAUSED, FLAGGED,
    PENDING_DELETION.

``account_update``
    Broad account-health events: VERIFIED_ACCOUNT, ACCOUNT_RESTRICTION,
    DISABLED_UPDATE, BANNED, etc.

No DB writes are performed — this module is alerting-only.  Each event emits a
structured ``log.warning`` / ``log.error`` / ``log.info`` line suitable for
log-based metrics and fires an optional Sentry capture (degrades gracefully when
``sentry_sdk`` is absent, mirroring ``_alert_dead_letter`` in
``whatsapp_delivery.dispatch.worker``).

**Meta subscription note (for operators):** these webhook fields must be
explicitly subscribed in the Meta App's WhatsApp webhook configuration
(App Dashboard → WhatsApp → Configuration → Webhook Fields) for Meta to
deliver them.  The code handles the events once they are subscribed; subscribing
only ``messages`` (the default) will never trigger this module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field name constants — used by both the parser and the app.py router.
# ---------------------------------------------------------------------------

FIELD_PHONE_QUALITY = "phone_number_quality_update"
FIELD_TEMPLATE_STATUS = "message_template_status_update"
FIELD_ACCOUNT_UPDATE = "account_update"

ACCOUNT_EVENT_FIELDS: frozenset[str] = frozenset({
    FIELD_PHONE_QUALITY,
    FIELD_TEMPLATE_STATUS,
    FIELD_ACCOUNT_UPDATE,
})

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountEvent:
    """Normalised representation of one account-level webhook change.

    ``field`` is the raw ``change.field`` string (one of the three constants
    above).  ``event`` is the primary event discriminator within that field
    (e.g. ``"FLAGGED"``, ``"APPROVED"``, ``"BANNED"``).  ``detail`` is a
    free-form dict of supporting attributes captured without interpretation;
    callers should treat it as read-only context for logging / alerting.
    """

    field: str
    event: str
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

# Phone-quality events that warrant elevated log level.
_PHONE_QUALITY_ERROR_EVENTS: frozenset[str] = frozenset({"FLAGGED", "DOWNGRADE"})

# Template status events that warrant elevated log level.
_TEMPLATE_ERROR_EVENTS: frozenset[str] = frozenset({"PAUSED", "REJECTED", "FLAGGED"})

# Account update events that warrant elevated log level.
_ACCOUNT_ERROR_EVENTS: frozenset[str] = frozenset({
    "ACCOUNT_RESTRICTION",
    "DISABLED_UPDATE",
    "BANNED",
})


def _is_error_event(ev: AccountEvent) -> bool:
    """Return True when the event warrants ``log.error`` (louder) alerting."""
    f = ev.field
    e = ev.event.upper()
    if f == FIELD_PHONE_QUALITY:
        return e in _PHONE_QUALITY_ERROR_EVENTS
    if f == FIELD_TEMPLATE_STATUS:
        return e in _TEMPLATE_ERROR_EVENTS
    if f == FIELD_ACCOUNT_UPDATE:
        return e in _ACCOUNT_ERROR_EVENTS
    return False  # unknown field — benign assumption


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _parse_phone_quality(value: dict[str, Any]) -> AccountEvent:
    """Parse a ``phone_number_quality_update`` change value."""
    event = str(value.get("event") or "UNKNOWN")
    detail: dict[str, Any] = {}
    if "display_phone_number" in value:
        detail["phone"] = value["display_phone_number"]
    if "current_limit" in value:
        detail["current_limit"] = value["current_limit"]
    return AccountEvent(field=FIELD_PHONE_QUALITY, event=event, detail=detail)


def _parse_template_status(value: dict[str, Any]) -> AccountEvent:
    """Parse a ``message_template_status_update`` change value."""
    event = str(value.get("event") or "UNKNOWN")
    detail: dict[str, Any] = {}
    if "message_template_name" in value:
        detail["template_name"] = value["message_template_name"]
    if "message_template_id" in value:
        detail["template_id"] = value["message_template_id"]
    if "reason" in value and value["reason"] is not None:
        detail["reason"] = value["reason"]
    return AccountEvent(field=FIELD_TEMPLATE_STATUS, event=event, detail=detail)


def _parse_account_update(value: dict[str, Any]) -> AccountEvent:
    """Parse an ``account_update`` change value."""
    event = str(value.get("event") or "UNKNOWN")
    detail: dict[str, Any] = {}
    # Capture any restriction / ban / violation info present
    for key in ("ban_info", "violation_info", "restriction_info"):
        if key in value and value[key] is not None:
            detail[key] = value[key]
    return AccountEvent(field=FIELD_ACCOUNT_UPDATE, event=event, detail=detail)


_FIELD_PARSERS = {
    FIELD_PHONE_QUALITY: _parse_phone_quality,
    FIELD_TEMPLATE_STATUS: _parse_template_status,
    FIELD_ACCOUNT_UPDATE: _parse_account_update,
}


def parse_account_events(payload: dict[str, Any]) -> list[AccountEvent]:
    """Walk a Meta webhook payload and extract account-level events.

    Returns one :class:`AccountEvent` per matching ``entry[].changes[]``
    entry.  Unknown or malformed changes are skipped defensively without
    raising, so a parse error here never disturbs concurrent message /
    status processing.

    Args:
        payload: The raw webhook JSON dict (same envelope as
            ``parse_incoming`` / ``parse_status_updates``).

    Returns:
        List of :class:`AccountEvent` objects (may be empty).
    """
    out: list[AccountEvent] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            change_field = change.get("field", "")
            if change_field not in ACCOUNT_EVENT_FIELDS:
                continue
            value = change.get("value") or {}
            parser = _FIELD_PARSERS.get(change_field)
            if parser is None:
                continue
            try:
                out.append(parser(value))
            except Exception:  # pragma: no cover — defensive; parsers are simple
                log.warning(
                    "account_handler: failed to parse change field=%r; skipping",
                    change_field,
                    exc_info=True,
                )
    return out


# ---------------------------------------------------------------------------
# Handler / alerter
# ---------------------------------------------------------------------------


def _emit_sentry(ev: AccountEvent) -> None:
    """Fire an optional Sentry capture for a single AccountEvent.

    Mirrors the ``_alert_dead_letter`` pattern in
    ``whatsapp_delivery.dispatch.worker``: import is guarded so the absence
    of ``sentry_sdk`` is not a runtime error.
    """
    try:
        import sentry_sdk  # noqa: PLC0415 — optional dep, import guarded

        scope_cm = getattr(sentry_sdk, "new_scope", None) or sentry_sdk.push_scope
        with scope_cm() as scope:
            scope.set_extra("field", ev.field)
            scope.set_extra("event", ev.event)
            for k, v in ev.detail.items():
                scope.set_extra(k, v)
            level = "error" if _is_error_event(ev) else "info"
            sentry_sdk.capture_message(
                f"whatsapp_account_event: field={ev.field} event={ev.event}",
                level=level,
            )
    except Exception:  # pragma: no cover — Sentry optional, best-effort
        pass


def handle_account_events(payload: dict[str, Any]) -> int:
    """Parse account-level webhook events and emit structured alerts.

    For each event:
    - ``log.error`` for quality DOWNGRADE/FLAGGED, template PAUSED/REJECTED/
      FLAGGED, and any account restriction/ban/disabled events.
    - ``log.info`` for benign events (quality UPGRADE/ONLINED, template
      APPROVED, account VERIFIED_ACCOUNT).
    - ``log.warning`` for anything else (unknown event strings).
    - Sentry capture at the matching level (if ``sentry_sdk`` is installed).

    A malformed ``payload`` (including a completely missing ``"entry"`` key)
    is handled gracefully: ``parse_account_events`` returns an empty list and
    this function returns 0.

    Args:
        payload: The raw webhook JSON dict.

    Returns:
        The number of account events handled (>= 0).
    """
    try:
        events = parse_account_events(payload)
    except Exception:  # pragma: no cover — parse_account_events is itself defensive
        log.warning(
            "account_handler: parse_account_events raised unexpectedly; "
            "dropping account events for this payload",
            exc_info=True,
        )
        return 0

    for ev in events:
        detail_str = " ".join(f"{k}={v!r}" for k, v in ev.detail.items())
        msg = "metric=whatsapp_account_event field=%s event=%s detail=%s"
        args = (ev.field, ev.event, detail_str or "-")

        if _is_error_event(ev):
            log.error(msg, *args)
        else:
            # Distinguish genuinely benign events from unknowns
            known_benign = (
                (ev.field == FIELD_PHONE_QUALITY and ev.event.upper() in ("UPGRADE", "ONLINED"))
                or (ev.field == FIELD_TEMPLATE_STATUS and ev.event.upper() == "APPROVED")
                or (ev.field == FIELD_ACCOUNT_UPDATE and ev.event.upper() == "VERIFIED_ACCOUNT")
            )
            if known_benign:
                log.info(msg, *args)
            else:
                log.warning(msg, *args)

        _emit_sentry(ev)

    return len(events)
