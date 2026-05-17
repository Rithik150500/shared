"""Tests for the Razorpay Customers + Offers API wrappers (Task 4.5)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from case_billing.razorpay_client.client import RazorpayHTTPClient
from case_billing.razorpay_client.customers import create_customer
from case_billing.razorpay_client.offers import create_offer


def _client() -> RazorpayHTTPClient:
    return RazorpayHTTPClient(key_id="k", key_secret="s")


@pytest.mark.asyncio
@respx.mock
async def test_create_customer_posts_expected_payload() -> None:
    route = respx.post("https://api.razorpay.com/v1/customers").mock(
        return_value=httpx.Response(
            200,
            json={"id": "cust_X", "name": "Asha", "contact": "+919999", "email": "a@x"},
        )
    )

    out = await create_customer(
        _client(),
        name="Asha",
        email="a@x",
        contact="+919999",
        notes={"user_id": "u_42"},
    )

    payload = json.loads(route.calls.last.request.read())
    assert payload == {
        "name": "Asha",
        "email": "a@x",
        "contact": "+919999",
        "notes": {"user_id": "u_42"},
    }
    assert out["id"] == "cust_X"


@pytest.mark.asyncio
@respx.mock
async def test_create_offer_posts_default_subscription_redemption() -> None:
    route = respx.post("https://api.razorpay.com/v1/offers").mock(
        return_value=httpx.Response(
            200,
            json={"id": "offer_chambers_half", "name": "Chambers Intro 50% Off"},
        )
    )

    out = await create_offer(
        _client(),
        name="Chambers Intro 50% Off",
        percent_off=50,
        max_count=1000,
    )

    payload = json.loads(route.calls.last.request.read())
    assert payload == {
        "name": "Chambers Intro 50% Off",
        "percent_off": 50,
        "max_count": 1000,
        "redemption_type": "subscription",
    }
    assert out["id"] == "offer_chambers_half"


@pytest.mark.asyncio
@respx.mock
async def test_create_offer_supports_alternate_redemption_type() -> None:
    route = respx.post("https://api.razorpay.com/v1/offers").mock(
        return_value=httpx.Response(200, json={"id": "offer_link_promo"})
    )

    await create_offer(
        _client(),
        name="Link 10% Off",
        percent_off=10,
        max_count=500,
        redemption_type="payment_link",
    )

    payload = json.loads(route.calls.last.request.read())
    assert payload["redemption_type"] == "payment_link"
