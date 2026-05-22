"""Fixtures local to the live test suite."""
import os
import pytest


@pytest.fixture(scope="session")
def sandbox_creds():
    """Razorpay sandbox API credentials. Skip the test if missing."""
    key = os.getenv("RAZORPAY_SANDBOX_API_KEY")
    secret = os.getenv("RAZORPAY_SANDBOX_API_SECRET")
    webhook_secret = os.getenv("RAZORPAY_SANDBOX_WEBHOOK_SECRET")
    if not (key and secret and webhook_secret):
        pytest.skip(
            "Razorpay sandbox credentials not set. "
            "Set RAZORPAY_SANDBOX_API_KEY, RAZORPAY_SANDBOX_API_SECRET, "
            "RAZORPAY_SANDBOX_WEBHOOK_SECRET to run."
        )
    return {
        "api_key": key,
        "api_secret": secret,
        "webhook_secret": webhook_secret,
    }
