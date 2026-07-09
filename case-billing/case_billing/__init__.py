"""case_billing — shared package for Munshi postpaid + Nowlez subscription billing.

Public API exports are placeholders to be filled as modules land.
"""

from __future__ import annotations

import sentry_sdk

from case_billing.config import BillingConfig
from case_billing.limits import NOWLEZ_FREE_TIER_CASE_CAP
from case_billing.errors import (
    BillingError,
    InvoiceNotFound,
    RazorpayApiError,
    SubscriptionNotActive,
    TierAlreadySelected,
    TrialAlreadyExpired,
    WebhookSignatureInvalid,
)

# Tag every Sentry event raised inside this package so triage can filter by
# `package:case_billing` across Munshi and Nowlez services.
sentry_sdk.set_tag("package", "case_billing")

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "BillingConfig",
    "NOWLEZ_FREE_TIER_CASE_CAP",
    "BillingError",
    "InvoiceNotFound",
    "RazorpayApiError",
    "SubscriptionNotActive",
    "TierAlreadySelected",
    "TrialAlreadyExpired",
    "WebhookSignatureInvalid",
]
