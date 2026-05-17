"""Tests for the Razorpay Invoices API wrapper (Task 4.3)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from case_billing.razorpay_client.client import RazorpayHTTPClient
from case_billing.razorpay_client.invoices import (
    create_invoice,
    fetch_invoice,
    void_invoice,
)


def _client() -> RazorpayHTTPClient:
    return RazorpayHTTPClient(key_id="k", key_secret="s")


INVOICE_RESPONSE = {
    "id": "inv_abc",
    "short_url": "https://rzp.io/i/inv_abc",
    "status": "issued",
    "amount": 50_000,
    "currency": "INR",
}


@pytest.mark.asyncio
@respx.mock
async def test_create_invoice_posts_expected_payload() -> None:
    route = respx.post("https://api.razorpay.com/v1/invoices").mock(
        return_value=httpx.Response(200, json=INVOICE_RESPONSE)
    )

    line_items = [
        {"name": "Cases processed (50 @ ₹10)", "amount": 50_000, "currency": "INR"},
    ]
    out = await create_invoice(
        _client(),
        customer_id="cust_X",
        line_items=line_items,
        due_by=1_705_000_000,
        notes={"period": "2026-04", "user_id": "u_1"},
    )

    payload = json.loads(route.calls.last.request.read())
    assert payload == {
        "type": "invoice",
        "customer_id": "cust_X",
        "line_items": line_items,
        "due_by": 1_705_000_000,
        "notes": {"period": "2026-04", "user_id": "u_1"},
    }
    assert out["id"] == "inv_abc"
    assert out["short_url"] == "https://rzp.io/i/inv_abc"
    assert out["status"] == "issued"


@pytest.mark.asyncio
@respx.mock
async def test_void_invoice_posts_to_void_endpoint() -> None:
    route = respx.post("https://api.razorpay.com/v1/invoices/inv_abc/void").mock(
        return_value=httpx.Response(200, json={**INVOICE_RESPONSE, "status": "cancelled"})
    )

    out = await void_invoice(_client(), "inv_abc")

    assert route.called
    assert out["status"] == "cancelled"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_invoice_returns_dict() -> None:
    route = respx.get("https://api.razorpay.com/v1/invoices/inv_abc").mock(
        return_value=httpx.Response(200, json=INVOICE_RESPONSE)
    )

    out = await fetch_invoice(_client(), "inv_abc")

    assert route.called
    assert out == INVOICE_RESPONSE
