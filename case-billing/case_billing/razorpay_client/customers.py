"""Razorpay Customers API wrapper.

Customers in Razorpay are reusable across Subscriptions, Invoices, and
Payment Links. We create one per Nowlez or Munshi user on first paid
interaction and reuse its ``customer_id`` for all later billing.
"""

from __future__ import annotations

from typing import Any

from case_billing.razorpay_client.client import RazorpayHTTPClient


async def create_customer(
    client: RazorpayHTTPClient,
    *,
    name: str,
    email: str,
    contact: str,
    notes: dict[str, Any],
) -> dict[str, Any]:
    """POST /v1/customers.

    Args:
        name: Display name; surfaced on invoices and receipts.
        email: Primary email; Razorpay sends invoice copies here when
            ``customer_notify=True`` is set on the parent resource.
        contact: E.164 phone number (Razorpay uses ``+`` prefix).
        notes: Free-form metadata, used to stash our internal user_id.
    """
    body: dict[str, Any] = {
        "name": name,
        "email": email,
        "contact": contact,
        "notes": notes,
    }
    return await client._request("POST", "/v1/customers", json_data=body)
