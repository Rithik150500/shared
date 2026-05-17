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

import re
import time
from typing import Any

import httpx

from case_billing.errors import RazorpayApiError
from case_billing.metrics import (
    billing_razorpay_api_calls_total,
    billing_razorpay_api_latency_seconds,
)

RAZORPAY_API_BASE = "https://api.razorpay.com"
DEFAULT_TIMEOUT_SECONDS = 30

# Razorpay paths embed resource IDs ("/v1/invoices/inv_ABC123/void"). For
# Prometheus label cardinality we collapse them to a template-style
# representation ("/v1/invoices/{id}/void") before emitting metrics.
# This regex matches the leading-letters-then-alphanumerics shape that
# Razorpay uses across every entity (`cust_`, `sub_`, `inv_`, `plan_`,
# `pay_`, `order_`, etc.).
_RAZORPAY_ID_PATTERN = re.compile(r"^[a-z]+_[A-Za-z0-9]+$")


def _path_template(path: str) -> str:
    """Collapse Razorpay entity ids in ``path`` to ``{id}``.

    Keeps the Prometheus label cardinality bounded — without this every
    distinct subscription_id / invoice_id would explode the time series.
    """
    parts = path.split("/")
    return "/".join("{id}" if _RAZORPAY_ID_PATTERN.match(p) else p for p in parts)


def _status_bucket(status_code: int) -> str:
    """Bucket the HTTP status into ``'2xx' / '4xx' / '5xx' / 'error'``."""
    if status_code == 0:
        return "error"
    return f"{status_code // 100}xx"


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

        # Sub-project E Task 18.2.2: emit api_calls + latency metrics on
        # every request, including failures. ``_endpoint`` is the
        # cardinality-bounded label; ``_status`` buckets to 2xx/4xx/5xx
        # so an actionable alert ("5xx rate > 1% over 5m") is cheap.
        _endpoint = _path_template(path)
        _t_start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout, auth=auth) as http:
                response = await http.request(method, url, json=json_data)
        except Exception:
            # Network-layer failure (timeout, DNS, TLS). Record as the
            # 'error' bucket so dashboards can split "Razorpay rejected"
            # from "we never got a response".
            billing_razorpay_api_calls_total.labels(
                endpoint=_endpoint, status="error",
            ).inc()
            billing_razorpay_api_latency_seconds.labels(
                endpoint=_endpoint,
            ).observe(time.monotonic() - _t_start)
            raise
        finally:
            # Latency is recorded on success here (the except branch
            # records its own and re-raises before reaching this).
            pass

        billing_razorpay_api_calls_total.labels(
            endpoint=_endpoint, status=_status_bucket(response.status_code),
        ).inc()
        billing_razorpay_api_latency_seconds.labels(
            endpoint=_endpoint,
        ).observe(time.monotonic() - _t_start)

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
