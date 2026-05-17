"""Razorpay Offers API wrapper.

Used exclusively at one-time launch setup to mint the intro-promo and
referrer-mutual offer IDs; production code paths *attach* these offers via
:func:`case_billing.razorpay_client.subscriptions.create_subscription` or
:func:`update_subscription_offer`, never create new ones at runtime.
"""

from __future__ import annotations

from typing import Any

from case_billing.razorpay_client.client import RazorpayHTTPClient


async def create_offer(
    client: RazorpayHTTPClient,
    *,
    name: str,
    percent_off: int,
    max_count: int,
    redemption_type: str = "subscription",
) -> dict[str, Any]:
    """POST /v1/offers.

    Args:
        name: Human-readable offer name (Razorpay dashboard label).
        percent_off: Discount percentage (e.g. 50 for the half-price
            intro promo).
        max_count: Total number of redemptions allowed across all users.
        redemption_type: ``"subscription"`` for subscription-bound offers
            (default, covers both intro promo and referrer-mutual benefit)
            or ``"payment_link"`` for one-shot link discounts.
    """
    body: dict[str, Any] = {
        "name": name,
        "percent_off": percent_off,
        "max_count": max_count,
        "redemption_type": redemption_type,
    }
    return await client._request("POST", "/v1/offers", json_data=body)
