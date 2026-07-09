"""The unified free-tier case cap constant.

Guards two things the design depends on:
1. The value is 5 (North Star v2).
2. It is importable WITHOUT the Razorpay secrets that ``BillingConfig()``
   requires — casepilot imports this at config-load time, and instantiating
   ``BillingConfig`` there (with secrets unset) would crash boot. Keeping the
   cap a plain module constant avoids that.
"""
from __future__ import annotations

import importlib


def test_free_tier_cap_is_five():
    from case_billing.limits import NOWLEZ_FREE_TIER_CASE_CAP

    assert NOWLEZ_FREE_TIER_CASE_CAP == 5


def test_cap_importable_without_billing_secrets(monkeypatch):
    for var in (
        "RAZORPAY_KEY_ID",
        "RAZORPAY_KEY_SECRET",
        "RAZORPAY_WEBHOOK_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    import case_billing.limits as lim

    importlib.reload(lim)
    assert lim.NOWLEZ_FREE_TIER_CASE_CAP == 5


def test_cap_exported_from_package():
    import case_billing

    assert case_billing.NOWLEZ_FREE_TIER_CASE_CAP == 5
