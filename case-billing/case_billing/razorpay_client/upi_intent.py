"""GST-compliant UPI Intent helpers.

Per spec Section "Payment Methods", the dedicated UPI button on the consent
screen MUST still settle through Razorpay so the GST invoice is generated
automatically and the funds land in the same reconciliation feed as every
other payment. Razorpay exposes this as the regular Payment Links endpoint
with ``upi_link=true``, which returns a UPI deep-link the WhatsApp client
can open directly.
"""

from __future__ import annotations

from typing import Any

from case_billing.razorpay_client.client import RazorpayHTTPClient


async def create_upi_payment_link(
    client: RazorpayHTTPClient,
    *,
    amount: int,
    customer: dict[str, Any],
    notes: dict[str, Any],
    currency: str = "INR",
) -> dict[str, Any]:
    """POST /v1/payment_links with ``{"upi_link": true}``.

    Args:
        amount: Amount in paise.
        customer: ``{"name", "contact", "email"}`` dict.
        notes: Metadata copied through to the Razorpay object.
        currency: ISO 4217 code; defaults to INR (UPI is INR-only today).
    """
    body: dict[str, Any] = {
        "amount": amount,
        "currency": currency,
        "upi_link": True,
        "customer": customer,
        "notes": notes,
    }
    return await client._request("POST", "/v1/payment_links", json_data=body)
