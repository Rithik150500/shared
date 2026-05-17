"""Razorpay Payment Links API wrapper.

Used by both Munshi (invoice-style postpaid receipts the customer can pay
without logging in) and the Nowlez "pay now" fallback when the
Subscriptions card-on-file flow rejects.

The spec keeps :func:`create_payment_link` as the only public surface for
generic links; UPI-intent variants live in :mod:`upi_intent` so the call
sites read self-documentingly.
"""

from __future__ import annotations

from typing import Any

from case_billing.razorpay_client.client import RazorpayHTTPClient


async def create_payment_link(
    client: RazorpayHTTPClient,
    *,
    amount: int,
    customer: dict[str, Any],
    notes: dict[str, Any],
    currency: str = "INR",
    accept_partial: bool = False,
    callback_url: str | None = None,
) -> dict[str, Any]:
    """POST /v1/payment_links.

    Args:
        amount: Amount in paise (smallest currency unit).
        customer: ``{"name", "contact", "email"}`` dict that Razorpay
            attaches to the link.
        notes: Free-form metadata persisted on the Razorpay object.
        currency: ISO 4217 code; defaults to INR per spec (only INR is
            supported today, but exposed so we don't have to rewrap when
            adding USD later).
        accept_partial: If True, the customer can pay any positive amount
            below ``amount`` (used by Munshi when offering split payment).
        callback_url: Optional URL Razorpay redirects to after payment;
            paired with ``callback_method="get"`` per Razorpay's own API.
    """
    body: dict[str, Any] = {
        "amount": amount,
        "currency": currency,
        "accept_partial": accept_partial,
        "customer": customer,
        "notes": notes,
    }
    if callback_url is not None:
        body["callback_url"] = callback_url
        body["callback_method"] = "get"
    return await client._request("POST", "/v1/payment_links", json_data=body)
