"""Prometheus metrics for the case-billing package (Sub-project E Task 18.2.1).

This module defines every counter, histogram, and gauge the case-billing
handlers emit. Handlers import the named objects directly and call
``.inc()``, ``.observe()``, or ``.set()`` — no string-based metric
lookups so typos fail at import time.

Naming convention: ``billing_<product>_<event>_<unit>{labels}``.

* ``product`` ∈ {munshi, nowlez, razorpay}.
* ``event`` describes the state-machine transition or external call.
* ``unit`` is ``total`` for counters, ``seconds`` for latency histograms,
  ``paise`` for monetary amounts (already INR-denominated minor units),
  ``count`` for instantaneous gauges.

The metric **shape** matches plan §18.2.1 (8 Munshi + 11 Nowlez + 5
Razorpay). The exact names + label sets are picked here based on the
handler call sites in this package; if a downstream Grafana dashboard
needs renames the central registration here is the only file to edit.

All metrics are registered against the default Prometheus registry so
the existing ``/metrics`` endpoint in the casepilot backend picks them
up automatically — see ``casepilot/backend/main.py::prometheus_metrics``.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram


# ---------------------------------------------------------------------------
# Munshi metrics (plan §18.2.1 — 8 metrics)
# ---------------------------------------------------------------------------

# (1) Number of anniversary invoices generated. Labelled by cycle day
# so the daily distribution is visible in Grafana — a spike on a single
# day-of-month indicates the cohort that signed up that day.
billing_munshi_invoices_generated_total = Counter(
    "billing_munshi_invoices_generated_total",
    "Munshi anniversary invoices successfully generated.",
    ["cycle_day"],
)

# (2) Money amount billed (paise). Sums across all users so the
# cumulative gross monthly revenue can be reconstructed from the
# counter delta.
billing_munshi_amount_paise_total = Counter(
    "billing_munshi_amount_paise_total",
    "Total Munshi invoice amount in paise (INR minor units).",
)

# (3) Invoices marked paid via webhook.
billing_munshi_invoices_paid_total = Counter(
    "billing_munshi_invoices_paid_total",
    "Munshi invoices marked paid (invoice.paid webhook).",
)

# (4) Invoices that hit the grace window (status='sent' → 'in_grace').
billing_munshi_invoices_in_grace_total = Counter(
    "billing_munshi_invoices_in_grace_total",
    "Munshi invoices that crossed the due date into the grace window.",
)

# (5) Users suspended after grace window expired.
billing_munshi_users_suspended_total = Counter(
    "billing_munshi_users_suspended_total",
    "Munshi users suspended after grace-window expiry.",
)

# (6) Users resumed (suspension → paid).
billing_munshi_users_resumed_total = Counter(
    "billing_munshi_users_resumed_total",
    "Munshi users restored from suspended status after payment.",
)

# (7) Reminders sent (D-5/D-3/D-0). Labelled by offset so the
# operations dashboard can show which leg of the dunning ladder is
# firing most.
billing_munshi_reminders_sent_total = Counter(
    "billing_munshi_reminders_sent_total",
    "Munshi payment-reminder templates sent.",
    ["offset"],
)

# (8) Invoice generation end-to-end latency (Razorpay round-trips +
# Postgres writes + WhatsApp enqueue). Histogram buckets tuned for the
# observed p50 ~300 ms / p99 ~3 s; tweak in Grafana if real-world traffic
# pulls the tail elsewhere.
billing_munshi_invoice_generation_seconds = Histogram(
    "billing_munshi_invoice_generation_seconds",
    "End-to-end latency for generate_anniversary_invoice (success path).",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)


# ---------------------------------------------------------------------------
# Nowlez metrics (plan §18.2.1 — 11 metrics)
# ---------------------------------------------------------------------------

# (1) Subscriptions created. Labelled by tier so the cohort split is
# visible without joining against the DB.
billing_nowlez_subscriptions_created_total = Counter(
    "billing_nowlez_subscriptions_created_total",
    "Nowlez subscriptions created via select_tier_and_subscribe.",
    ["tier"],
)

# (2) Subscriptions activated (subscription.activated webhook).
billing_nowlez_subscriptions_activated_total = Counter(
    "billing_nowlez_subscriptions_activated_total",
    "Nowlez subscriptions transitioned to status='active'.",
    ["tier"],
)

# (3) Subscriptions marked past_due (subscription.halted webhook).
billing_nowlez_subscriptions_past_due_total = Counter(
    "billing_nowlez_subscriptions_past_due_total",
    "Nowlez subscriptions transitioned to status='past_due'.",
    ["tier"],
)

# (4) Subscriptions cancelled.
billing_nowlez_subscriptions_cancelled_total = Counter(
    "billing_nowlez_subscriptions_cancelled_total",
    "Nowlez subscriptions cancelled.",
    ["tier"],
)

# (5) Trials started (signup-time 30-day Chambers trial).
billing_nowlez_trials_started_total = Counter(
    "billing_nowlez_trials_started_total",
    "30-day Chambers trials started at signup.",
)

# (6) Trials expired via the admin/cron `expire_trial` path. Distinct
# from the natural "user picked a tier" exit because lapsed trials feed
# the fallback paths below.
billing_nowlez_trials_expired_total = Counter(
    "billing_nowlez_trials_expired_total",
    "30-day Chambers trials expired (cron / admin call).",
)

# (7) Day-31 fallback to Munshi (default policy).
billing_nowlez_trial_fallback_munshi_total = Counter(
    "billing_nowlez_trial_fallback_munshi_total",
    "Lapsed-trial users handed off to Munshi postpaid billing.",
)

# (8) Day-31 fallback freeze (alternative policy).
billing_nowlez_trial_fallback_freeze_total = Counter(
    "billing_nowlez_trial_fallback_freeze_total",
    "Lapsed-trial users hard-stopped via freeze_account policy.",
)

# (9) Day-31 fallback no-ops (raced tier pick or zero cases). Includes
# the reason so triage can distinguish the two.
billing_nowlez_trial_fallback_noop_total = Counter(
    "billing_nowlez_trial_fallback_noop_total",
    "Lapsed-trial fallback skipped (raced tier-pick or zero cases).",
    ["reason"],
)

# (10) Intro-promo applications. Labelled by tier (only chambers and
# counsel are eligible per spec).
billing_nowlez_intro_promo_applied_total = Counter(
    "billing_nowlez_intro_promo_applied_total",
    "Intro-promo offers attached at subscription creation.",
    ["tier"],
)

# (11) Active subscriptions gauge — point-in-time count, set by the
# cron after each tick. Useful for capacity planning and as a smoke
# test that the cron is running at all.
billing_nowlez_active_subscriptions = Gauge(
    "billing_nowlez_active_subscriptions",
    "Nowlez subscriptions currently in status='active'.",
    ["tier"],
)


# ---------------------------------------------------------------------------
# Razorpay metrics (plan §18.2.1 — 5 metrics)
# ---------------------------------------------------------------------------

# (1) API calls. Labelled by endpoint path-template and HTTP status
# class so a 5xx spike on a specific endpoint is obvious.
billing_razorpay_api_calls_total = Counter(
    "billing_razorpay_api_calls_total",
    "Razorpay REST API requests.",
    ["endpoint", "status"],
)

# (2) Latency histogram per endpoint.
billing_razorpay_api_latency_seconds = Histogram(
    "billing_razorpay_api_latency_seconds",
    "Razorpay REST API request latency in seconds.",
    ["endpoint"],
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# (3) Webhook events received and parsed. Labelled by event_type so
# the volume of subscription.activated vs invoice.paid is visible.
billing_razorpay_webhooks_received_total = Counter(
    "billing_razorpay_webhooks_received_total",
    "Razorpay webhook events received and signature-verified.",
    ["event_type"],
)

# (4) Webhook signature failures — security canary. A non-zero rate
# indicates either a misconfigured RAZORPAY_WEBHOOK_SECRET or an actual
# replay/forgery attempt. Sentry alert should fire on any > 0.
billing_razorpay_webhook_signature_failures_total = Counter(
    "billing_razorpay_webhook_signature_failures_total",
    "Razorpay webhook requests rejected for invalid signature.",
)

# (5) Webhook events skipped because the razorpay_event_id was already
# in payment_events. Healthy non-zero baseline (Razorpay retries are
# common); a spike on a single event_id indicates a stuck handler.
billing_razorpay_webhook_duplicate_total = Counter(
    "billing_razorpay_webhook_duplicate_total",
    "Razorpay webhook events skipped as duplicates (idempotency hit).",
    ["event_type"],
)


__all__ = [
    # Munshi
    "billing_munshi_invoices_generated_total",
    "billing_munshi_amount_paise_total",
    "billing_munshi_invoices_paid_total",
    "billing_munshi_invoices_in_grace_total",
    "billing_munshi_users_suspended_total",
    "billing_munshi_users_resumed_total",
    "billing_munshi_reminders_sent_total",
    "billing_munshi_invoice_generation_seconds",
    # Nowlez
    "billing_nowlez_subscriptions_created_total",
    "billing_nowlez_subscriptions_activated_total",
    "billing_nowlez_subscriptions_past_due_total",
    "billing_nowlez_subscriptions_cancelled_total",
    "billing_nowlez_trials_started_total",
    "billing_nowlez_trials_expired_total",
    "billing_nowlez_trial_fallback_munshi_total",
    "billing_nowlez_trial_fallback_freeze_total",
    "billing_nowlez_trial_fallback_noop_total",
    "billing_nowlez_intro_promo_applied_total",
    "billing_nowlez_active_subscriptions",
    # Razorpay
    "billing_razorpay_api_calls_total",
    "billing_razorpay_api_latency_seconds",
    "billing_razorpay_webhooks_received_total",
    "billing_razorpay_webhook_signature_failures_total",
    "billing_razorpay_webhook_duplicate_total",
]
