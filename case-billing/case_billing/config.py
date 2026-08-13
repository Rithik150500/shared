"""BillingConfig — pydantic-settings model for the case-billing package.

Mirrors spec Section 2.5 field-for-field. All secrets are read from the
process environment (or `.env` for local dev); defaults are only set for
non-secret tunables.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class BillingConfig(BaseSettings):
    """Runtime configuration for Munshi + Nowlez billing.

    Razorpay credentials and plan/offer IDs MUST be supplied via env vars; the
    behavioural knobs below have safe defaults that match the launch spec.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Razorpay credentials -----------------------------------------------
    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str

    # --- Razorpay plan IDs (Subscriptions API) ------------------------------
    razorpay_plan_id_advocate_monthly: str
    razorpay_plan_id_counsel_monthly: str
    razorpay_plan_id_chambers_monthly: str
    razorpay_plan_id_chambers_quarterly: str
    razorpay_plan_id_chambers_yearly: str

    # --- Razorpay offer IDs (intro promo) -----------------------------------
    # Optional: the Razorpay Offers API is an account-tier-gated feature; not
    # every merchant plan has it enabled. When absent, intro-promo eligible
    # subscriptions are simply created without an offer attachment.
    # Consumers (case_billing.nowlez.promos.get_intro_offer_id) already
    # return None for the advocate tier — the None path was always supported,
    # we're just making the schema match the runtime contract.
    razorpay_offer_id_chambers_half_off: str | None = None
    razorpay_offer_id_counsel_half_off: str | None = None

    # --- Munshi postpaid tunables -------------------------------------------
    # Munshi is bundled into the Nowlez plan (owner decision 2026-08-08). There
    # is no separate per-case charge, so the price is 0 and invoice generation
    # produces nothing to collect. The invoice cron is additionally gated off by
    # MUNSHI_BILLING_ENABLED; this makes the price itself harmless if that flag
    # is ever flipped.
    munshi_price_per_case_paise: int = 0
    munshi_case_cap: int = 200
    munshi_grace_period_days: int = 7

    # --- Nowlez subscription tunables ---------------------------------------
    nowlez_trial_duration_days: int = 30
    nowlez_lapsed_trial_action: str = "fallback_to_munshi"

    # --- Cross-product policies ---------------------------------------------
    skip_munshi_billing_for_nowlez_subscribers: bool = True

    # --- Invoice + retry policy --------------------------------------------
    invoice_due_days: int = 7
    razorpay_payment_retry_count: int = 3

    # --- Kill switches ------------------------------------------------------
    munshi_billing_disabled: bool = False
