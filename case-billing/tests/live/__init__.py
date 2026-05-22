"""Live tests that hit the Razorpay sandbox.

These are skipped by default. Run explicitly via:

    pytest -m live shared/case-billing/tests/live/

Requires env vars:
    RAZORPAY_SANDBOX_API_KEY
    RAZORPAY_SANDBOX_API_SECRET
    RAZORPAY_SANDBOX_WEBHOOK_SECRET
"""
