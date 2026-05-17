"""Smoke test: package imports cleanly and BillingConfig constructs with env."""

from __future__ import annotations

import pytest


# All required (non-defaulted) BillingConfig fields. Defaults documented in
# the plan's Section 2.5 are *not* set here, so we exercise the env-loader.
REQUIRED_ENV = {
    "RAZORPAY_KEY_ID": "rzp_test_key",
    "RAZORPAY_KEY_SECRET": "rzp_test_secret",
    "RAZORPAY_WEBHOOK_SECRET": "rzp_webhook_secret",
    "RAZORPAY_PLAN_ID_ADVOCATE_MONTHLY": "plan_advocate_m",
    "RAZORPAY_PLAN_ID_COUNSEL_MONTHLY": "plan_counsel_m",
    "RAZORPAY_PLAN_ID_CHAMBERS_MONTHLY": "plan_chambers_m",
    "RAZORPAY_PLAN_ID_CHAMBERS_QUARTERLY": "plan_chambers_q",
    "RAZORPAY_PLAN_ID_CHAMBERS_YEARLY": "plan_chambers_y",
    "RAZORPAY_OFFER_ID_CHAMBERS_HALF_OFF": "offer_chambers_half",
    "RAZORPAY_OFFER_ID_COUNSEL_HALF_OFF": "offer_counsel_half",
}


def test_package_imports() -> None:
    """Top-level imports resolve and surface the public API."""
    import case_billing

    assert case_billing.__version__ == "0.1.0"
    assert hasattr(case_billing, "BillingConfig")
    assert hasattr(case_billing, "BillingError")


def test_billing_config_constructs_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """BillingConfig populates from env vars and applies the spec defaults."""
    # Block any stray `.env` from a developer's working tree leaking values in.
    monkeypatch.chdir(tmpdir := __import__("tempfile").mkdtemp())

    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    from case_billing import BillingConfig

    config = BillingConfig()

    # Required fields land verbatim.
    assert config.razorpay_key_id == "rzp_test_key"
    assert config.razorpay_key_secret == "rzp_test_secret"
    assert config.razorpay_webhook_secret == "rzp_webhook_secret"
    assert config.razorpay_plan_id_chambers_yearly == "plan_chambers_y"
    assert config.razorpay_offer_id_counsel_half_off == "offer_counsel_half"

    # Spec defaults (Section 2.5) take effect when the env doesn't override them.
    assert config.munshi_price_per_case_paise == 1000
    assert config.munshi_case_cap == 200
    assert config.munshi_grace_period_days == 7
    assert config.nowlez_trial_duration_days == 30
    assert config.nowlez_lapsed_trial_action == "fallback_to_munshi"
    assert config.skip_munshi_billing_for_nowlez_subscribers is True
    assert config.invoice_due_days == 7
    assert config.razorpay_payment_retry_count == 3
    assert config.munshi_billing_disabled is False

    # tmpdir kept alive by closure; cleanup not required for the smoke test.
    _ = tmpdir


def test_error_hierarchy() -> None:
    """All billing errors share the BillingError base for blanket-catch use."""
    from case_billing.errors import (
        BillingError,
        InvoiceNotFound,
        RazorpayApiError,
        SubscriptionNotActive,
        TierAlreadySelected,
        TrialAlreadyExpired,
        WebhookSignatureInvalid,
    )

    for exc in (
        InvoiceNotFound,
        RazorpayApiError,
        SubscriptionNotActive,
        TierAlreadySelected,
        TrialAlreadyExpired,
        WebhookSignatureInvalid,
    ):
        assert issubclass(exc, BillingError), exc
