"""Tests for the base RazorpayHTTPClient (Task 4.1).

Mocks the Razorpay REST API with respx at the httpx transport layer; verifies
basic-auth, JSON body serialization, 30s timeout, and error-wrapping behaviour.
"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from case_billing.errors import RazorpayApiError
from case_billing.razorpay_client.client import RazorpayHTTPClient


def _make_client() -> RazorpayHTTPClient:
    return RazorpayHTTPClient(key_id="rzp_test_id", key_secret="rzp_test_secret")


def _expected_basic_auth_header(key_id: str, key_secret: str) -> str:
    raw = f"{key_id}:{key_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


@pytest.mark.asyncio
@respx.mock
async def test_request_uses_basic_auth_header() -> None:
    """Every outbound call carries the HTTP Basic auth header derived from creds."""
    route = respx.get("https://api.razorpay.com/v1/payments/pay_X").mock(
        return_value=httpx.Response(200, json={"id": "pay_X", "status": "captured"})
    )

    client = _make_client()
    body = await client._request("GET", "/v1/payments/pay_X")

    assert body == {"id": "pay_X", "status": "captured"}
    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == _expected_basic_auth_header(
        "rzp_test_id", "rzp_test_secret"
    )


@pytest.mark.asyncio
@respx.mock
async def test_request_serializes_json_body_for_post() -> None:
    """POSTs forward `json_data` as the JSON body with the right content-type."""
    route = respx.post("https://api.razorpay.com/v1/customers").mock(
        return_value=httpx.Response(200, json={"id": "cust_1"})
    )

    body = await _make_client()._request(
        "POST", "/v1/customers", json_data={"name": "Asha", "contact": "+91999"}
    )

    assert body == {"id": "cust_1"}
    sent = route.calls.last.request
    assert sent.headers["content-type"].startswith("application/json")
    # httpx emits compact JSON (no separators) — decode to compare semantically.
    import json as _json

    assert _json.loads(sent.read()) == {"name": "Asha", "contact": "+91999"}


@pytest.mark.asyncio
@respx.mock
async def test_request_wraps_4xx_in_razorpay_api_error() -> None:
    """Client 4xx (e.g. bad arguments) raises RazorpayApiError carrying the body."""
    respx.post("https://api.razorpay.com/v1/subscriptions").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"code": "BAD_REQUEST_ERROR", "description": "missing plan_id"}},
        )
    )

    with pytest.raises(RazorpayApiError) as exc_info:
        await _make_client()._request("POST", "/v1/subscriptions", json_data={})

    err = exc_info.value
    assert err.status_code == 400
    assert "missing plan_id" in str(err)


@pytest.mark.asyncio
@respx.mock
async def test_request_wraps_5xx_in_razorpay_api_error() -> None:
    """Razorpay 5xx errors raise RazorpayApiError too (retry decision lives upstream)."""
    respx.get("https://api.razorpay.com/v1/payments/pay_dead").mock(
        return_value=httpx.Response(503, text="upstream busy")
    )

    with pytest.raises(RazorpayApiError) as exc_info:
        await _make_client()._request("GET", "/v1/payments/pay_dead")

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
@respx.mock
async def test_request_passes_when_2xx_with_empty_body() -> None:
    """Empty/204 bodies do not crash the JSON decoder — client returns {} sentinel."""
    respx.post("https://api.razorpay.com/v1/invoices/inv_1/void").mock(
        return_value=httpx.Response(200, text="")
    )
    out = await _make_client()._request("POST", "/v1/invoices/inv_1/void")
    assert out == {}


def test_client_defaults_to_thirty_second_timeout() -> None:
    """Timeout is wired exactly per spec (30 seconds). Constructed lazily."""
    client = _make_client()
    assert client.timeout_seconds == 30
