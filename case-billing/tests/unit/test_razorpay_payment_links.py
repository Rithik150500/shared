"""Tests for the Razorpay Payment Links + UPI Intent wrappers (Task 4.4)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from case_billing.razorpay_client.client import RazorpayHTTPClient
from case_billing.razorpay_client.payment_links import create_payment_link
from case_billing.razorpay_client.upi_intent import create_upi_payment_link


def _client() -> RazorpayHTTPClient:
    return RazorpayHTTPClient(key_id="k", key_secret="s")


LINK_RESPONSE = {
    "id": "plink_abc",
    "short_url": "https://rzp.io/i/plink_abc",
    "amount": 50_000,
    "currency": "INR",
    "status": "created",
}


@pytest.mark.asyncio
@respx.mock
async def test_create_payment_link_posts_default_payload() -> None:
    route = respx.post("https://api.razorpay.com/v1/payment_links").mock(
        return_value=httpx.Response(200, json=LINK_RESPONSE)
    )

    customer = {"name": "Asha", "contact": "+919999999999", "email": "a@x"}
    out = await create_payment_link(
        _client(),
        amount=50_000,
        customer=customer,
        notes={"period": "2026-04"},
    )

    payload = json.loads(route.calls.last.request.read())
    assert payload == {
        "amount": 50_000,
        "currency": "INR",
        "accept_partial": False,
        "customer": customer,
        "notes": {"period": "2026-04"},
    }
    assert out["id"] == "plink_abc"


@pytest.mark.asyncio
@respx.mock
async def test_create_payment_link_accept_partial_and_callback() -> None:
    route = respx.post("https://api.razorpay.com/v1/payment_links").mock(
        return_value=httpx.Response(200, json=LINK_RESPONSE)
    )

    await create_payment_link(
        _client(),
        amount=200_000,
        customer={"name": "x", "contact": "+9100", "email": "x@y"},
        notes={},
        accept_partial=True,
        callback_url="https://nowlez.in/billing/return",
    )

    payload = json.loads(route.calls.last.request.read())
    assert payload["accept_partial"] is True
    assert payload["callback_url"] == "https://nowlez.in/billing/return"
    assert payload["callback_method"] == "get"


@pytest.mark.asyncio
@respx.mock
async def test_create_upi_payment_link_sets_upi_link_flag() -> None:
    """UPI intent goes through the same /v1/payment_links endpoint w/ upi_link=true."""
    route = respx.post("https://api.razorpay.com/v1/payment_links").mock(
        return_value=httpx.Response(200, json={**LINK_RESPONSE, "upi_link": True})
    )

    customer = {"name": "Asha", "contact": "+919999999999", "email": "a@x"}
    out = await create_upi_payment_link(
        _client(),
        amount=10_000,
        customer=customer,
        notes={"period": "2026-04"},
    )

    payload = json.loads(route.calls.last.request.read())
    assert payload == {
        "amount": 10_000,
        "currency": "INR",
        "upi_link": True,
        "customer": customer,
        "notes": {"period": "2026-04"},
    }
    assert out["id"] == "plink_abc"
