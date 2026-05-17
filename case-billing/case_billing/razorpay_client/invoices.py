"""Razorpay Invoices API wrapper (used by Munshi postpaid).

Munshi bills the previous month's case usage as a one-off Razorpay invoice,
so the surface is limited to: create, void (sub-project C's mid-cycle
correction path), and fetch (reconciliation).

Returns plain dicts rather than a dataclass because Munshi's persistence
layer stores Razorpay's whole response in a JSONB column for later
inspection during disputes; the cost of a dataclass here is wasted churn.
"""

from __future__ import annotations

from typing import Any

from case_billing.razorpay_client.client import RazorpayHTTPClient


async def create_invoice(
    client: RazorpayHTTPClient,
    *,
    customer_id: str,
    line_items: list[dict[str, Any]],
    due_by: int,
    notes: dict[str, Any],
) -> dict[str, Any]:
    """POST /v1/invoices.

    Args:
        customer_id: Razorpay customer_id (created via
            :func:`case_billing.razorpay_client.customers.create_customer`).
        line_items: List of ``{"name", "amount", "currency"}`` dicts; Munshi
            collapses each billing period to one line item (case count × ₹10)
            so the line list is typically length 1.
        due_by: UNIX epoch (seconds) when the invoice falls due; spec default
            is 7 days after issue.
        notes: Metadata copied through to the Razorpay object (period_yyyymm,
            user_id) so webhook handlers can correlate without DB round-trips.
    """
    body: dict[str, Any] = {
        "type": "invoice",
        "customer_id": customer_id,
        "line_items": line_items,
        "due_by": due_by,
        "notes": notes,
    }
    return await client._request("POST", "/v1/invoices", json_data=body)


async def void_invoice(
    client: RazorpayHTTPClient,
    invoice_id: str,
) -> dict[str, Any]:
    """POST /v1/invoices/{id}/void.

    Used by sub-project C's mid-cycle void path (e.g. when a Nowlez upgrade
    retroactively eliminates a Munshi invoice that had already been issued).
    """
    return await client._request("POST", f"/v1/invoices/{invoice_id}/void")


async def fetch_invoice(
    client: RazorpayHTTPClient,
    invoice_id: str,
) -> dict[str, Any]:
    """GET /v1/invoices/{id} for reconciliation against our local row."""
    return await client._request("GET", f"/v1/invoices/{invoice_id}")
