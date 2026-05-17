"""Async httpx wrapper for the Razorpay REST API.

Single responsibility: bridge between high-level domain wrappers (Subscriptions,
Invoices, Payment Links, etc.) and Razorpay's REST endpoints. Handles:

  * HTTP Basic auth via the (key_id, key_secret) pair.
  * 30-second per-request timeout (matches spec Section 2.5).
  * Non-2xx → :class:`case_billing.errors.RazorpayApiError` wrapping.
  * Empty-body 2xx responses (e.g. some POST `/void` endpoints) yield ``{}``.

The wrapper intentionally does NOT implement retries; that responsibility lives
in the queue layer so transient failures get exponential back-off without
double-charging customers on idempotent POSTs.
"""

from __future__ import annotations

from typing import Any

import httpx

from case_billing.errors import RazorpayApiError

RAZORPAY_API_BASE = "https://api.razorpay.com"
DEFAULT_TIMEOUT_SECONDS = 30


class RazorpayHTTPClient:
    """Thin async wrapper around Razorpay's REST endpoints."""

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        *,
        base_url: str = RAZORPAY_API_BASE,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.key_id = key_id
        self.key_secret = key_secret
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue an authenticated HTTP request and return the parsed JSON body.

        Args:
            method: HTTP verb (``GET``, ``POST``, ``PATCH``, ``DELETE``).
            path: API path beginning with ``/v1/...`` (joined onto base_url).
            json_data: Optional dict serialized as the JSON body.

        Returns:
            The decoded JSON response, or ``{}`` if the response body is empty.

        Raises:
            RazorpayApiError: If the server returns any non-2xx status. The
                instance carries ``status_code`` and ``body`` for upstream
                inspection (retry hints, log payload, etc.).
        """
        url = f"{self.base_url}{path}"
        timeout = httpx.Timeout(self.timeout_seconds)
        auth = (self.key_id, self.key_secret)

        async with httpx.AsyncClient(timeout=timeout, auth=auth) as http:
            response = await http.request(method, url, json=json_data)

        if response.status_code >= 400:
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise RazorpayApiError(
                f"Razorpay {method} {path} returned {response.status_code}: {body!r}",
                status_code=response.status_code,
                body=body,
            )

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            # Razorpay returned 2xx with non-JSON content; preserve as text.
            return {"raw": response.text}
