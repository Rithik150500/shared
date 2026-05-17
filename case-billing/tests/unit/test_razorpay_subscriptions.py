"""Tests for the Razorpay Subscriptions API wrapper (Task 4.2)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from case_billing.razorpay_client.client import RazorpayHTTPClient
from case_billing.razorpay_client.subscriptions import (
    RazorpaySubscription,
    cancel_subscription,
    create_subscription,
    fetch_subscription,
    update_subscription_offer,
)


def _client() -> RazorpayHTTPClient:
    return RazorpayHTTPClient(key_id="k", key_secret="s")


SUB_RESPONSE = {
    "id": "sub_abc123",
    "short_url": "https://rzp.io/i/abc123",
    "status": "created",
    "current_start": 1_700_000_000,
    "current_end": 1_702_592_000,
    "notes": {"user_id": "u_42", "tier": "chambers"},
}


@pytest.mark.asyncio
@respx.mock
async def test_create_subscription_posts_expected_payload() -> None:
    route = respx.post("https://api.razorpay.com/v1/subscriptions").mock(
        return_value=httpx.Response(200, json=SUB_RESPONSE)
    )

    sub = await create_subscription(
        _client(),
        plan_id="plan_chambers_m",
        customer_notify=True,
        total_count=12,
        notes={"user_id": "u_42", "tier": "chambers"},
    )

    assert route.called
    payload = json.loads(route.calls.last.request.read())
    assert payload == {
        "plan_id": "plan_chambers_m",
        "customer_notify": 1,
        "total_count": 12,
        "notes": {"user_id": "u_42", "tier": "chambers"},
    }
    assert isinstance(sub, RazorpaySubscription)
    assert sub.id == "sub_abc123"
    assert sub.short_url == "https://rzp.io/i/abc123"
    assert sub.status == "created"
    assert sub.current_start == 1_700_000_000
    assert sub.current_end == 1_702_592_000
    assert sub.notes == {"user_id": "u_42", "tier": "chambers"}


@pytest.mark.asyncio
@respx.mock
async def test_create_subscription_includes_offer_id_when_supplied() -> None:
    route = respx.post("https://api.razorpay.com/v1/subscriptions").mock(
        return_value=httpx.Response(200, json=SUB_RESPONSE)
    )

    await create_subscription(
        _client(),
        plan_id="plan_chambers_m",
        customer_notify=False,
        total_count=24,
        notes={},
        offer_id="offer_chambers_half_off",
    )

    payload = json.loads(route.calls.last.request.read())
    assert payload["offer_id"] == "offer_chambers_half_off"
    assert payload["customer_notify"] == 0  # False maps to 0 per Razorpay spec


@pytest.mark.asyncio
@respx.mock
async def test_cancel_subscription_defaults_to_cancel_at_cycle_end() -> None:
    route = respx.post("https://api.razorpay.com/v1/subscriptions/sub_X/cancel").mock(
        return_value=httpx.Response(200, json={**SUB_RESPONSE, "status": "cancelled"})
    )

    sub = await cancel_subscription(_client(), "sub_X")

    assert route.called
    payload = json.loads(route.calls.last.request.read())
    assert payload == {"cancel_at_cycle_end": 1}
    assert sub.status == "cancelled"


@pytest.mark.asyncio
@respx.mock
async def test_cancel_subscription_can_cancel_immediately() -> None:
    route = respx.post("https://api.razorpay.com/v1/subscriptions/sub_X/cancel").mock(
        return_value=httpx.Response(200, json={**SUB_RESPONSE, "status": "cancelled"})
    )

    await cancel_subscription(_client(), "sub_X", cancel_at_cycle_end=False)

    payload = json.loads(route.calls.last.request.read())
    assert payload == {"cancel_at_cycle_end": 0}


@pytest.mark.asyncio
@respx.mock
async def test_update_subscription_offer_posts_offer_id() -> None:
    route = respx.patch("https://api.razorpay.com/v1/subscriptions/sub_X").mock(
        return_value=httpx.Response(200, json=SUB_RESPONSE)
    )

    await update_subscription_offer(_client(), "sub_X", offer_id="offer_mutual")

    payload = json.loads(route.calls.last.request.read())
    assert payload == {"offer_id": "offer_mutual"}


@pytest.mark.asyncio
@respx.mock
async def test_fetch_subscription_returns_dataclass() -> None:
    route = respx.get("https://api.razorpay.com/v1/subscriptions/sub_abc123").mock(
        return_value=httpx.Response(200, json=SUB_RESPONSE)
    )

    sub = await fetch_subscription(_client(), "sub_abc123")

    assert route.called
    assert sub.id == "sub_abc123"
    assert sub.notes == {"user_id": "u_42", "tier": "chambers"}
