"""Smoke tests for ``case_billing.metrics`` (Sub-project E Task 18.2.1).

Confirms the named metric objects exist with the expected shape (counter
vs gauge vs histogram) and labels. Handler-side emission is covered by
the per-module test suites; this file is the canary that ensures the
metric registration itself doesn't drift out of band.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

from case_billing import metrics as m


# ---------------------------------------------------------------------------
# Munshi metrics — 8 per plan §18.2.1
# ---------------------------------------------------------------------------


def test_munshi_metric_shapes() -> None:
    assert isinstance(m.billing_munshi_invoices_generated_total, Counter)
    assert isinstance(m.billing_munshi_amount_paise_total, Counter)
    assert isinstance(m.billing_munshi_invoices_paid_total, Counter)
    assert isinstance(m.billing_munshi_invoices_in_grace_total, Counter)
    assert isinstance(m.billing_munshi_users_suspended_total, Counter)
    assert isinstance(m.billing_munshi_users_resumed_total, Counter)
    assert isinstance(m.billing_munshi_reminders_sent_total, Counter)
    assert isinstance(m.billing_munshi_invoice_generation_seconds, Histogram)


def test_munshi_label_sets() -> None:
    """Cardinality-bounded labels match the handler call sites."""
    # Labelled counters expose `_labelnames` (list[str]).
    assert m.billing_munshi_invoices_generated_total._labelnames == ("cycle_day",)
    assert m.billing_munshi_reminders_sent_total._labelnames == ("offset",)


# ---------------------------------------------------------------------------
# Nowlez metrics — 11 per plan §18.2.1
# ---------------------------------------------------------------------------


def test_nowlez_metric_shapes() -> None:
    assert isinstance(m.billing_nowlez_subscriptions_created_total, Counter)
    assert isinstance(m.billing_nowlez_subscriptions_activated_total, Counter)
    assert isinstance(m.billing_nowlez_subscriptions_past_due_total, Counter)
    assert isinstance(m.billing_nowlez_subscriptions_cancelled_total, Counter)
    assert isinstance(m.billing_nowlez_trials_started_total, Counter)
    assert isinstance(m.billing_nowlez_trials_expired_total, Counter)
    assert isinstance(m.billing_nowlez_trial_fallback_munshi_total, Counter)
    assert isinstance(m.billing_nowlez_trial_fallback_freeze_total, Counter)
    assert isinstance(m.billing_nowlez_trial_fallback_noop_total, Counter)
    assert isinstance(m.billing_nowlez_intro_promo_applied_total, Counter)
    assert isinstance(m.billing_nowlez_active_subscriptions, Gauge)


def test_nowlez_label_sets() -> None:
    assert m.billing_nowlez_subscriptions_created_total._labelnames == ("tier",)
    assert m.billing_nowlez_subscriptions_activated_total._labelnames == ("tier",)
    assert m.billing_nowlez_subscriptions_past_due_total._labelnames == ("tier",)
    assert m.billing_nowlez_subscriptions_cancelled_total._labelnames == ("tier",)
    assert m.billing_nowlez_intro_promo_applied_total._labelnames == ("tier",)
    assert m.billing_nowlez_trial_fallback_noop_total._labelnames == ("reason",)
    assert m.billing_nowlez_active_subscriptions._labelnames == ("tier",)


# ---------------------------------------------------------------------------
# Razorpay metrics — 5 per plan §18.2.1
# ---------------------------------------------------------------------------


def test_razorpay_metric_shapes() -> None:
    assert isinstance(m.billing_razorpay_api_calls_total, Counter)
    assert isinstance(m.billing_razorpay_api_latency_seconds, Histogram)
    assert isinstance(m.billing_razorpay_webhooks_received_total, Counter)
    assert isinstance(m.billing_razorpay_webhook_signature_failures_total, Counter)
    assert isinstance(m.billing_razorpay_webhook_duplicate_total, Counter)


def test_razorpay_label_sets() -> None:
    assert m.billing_razorpay_api_calls_total._labelnames == ("endpoint", "status")
    assert m.billing_razorpay_api_latency_seconds._labelnames == ("endpoint",)
    assert m.billing_razorpay_webhooks_received_total._labelnames == ("event_type",)
    assert m.billing_razorpay_webhook_duplicate_total._labelnames == ("event_type",)


# ---------------------------------------------------------------------------
# Plan-mandated count check (8 + 11 + 5 = 24)
# ---------------------------------------------------------------------------


def test_total_metric_count_matches_plan() -> None:
    """Plan §18.2.1 mandates 8 Munshi + 11 Nowlez + 5 Razorpay = 24 metrics."""
    assert len(m.__all__) == 8 + 11 + 5
