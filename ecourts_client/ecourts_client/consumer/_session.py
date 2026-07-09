"""e-Jagriti (Consumer forum) plain-JSON session.

Unlike the eCourts client there is **no** AES envelope and **no** JWT: the
tracking endpoints under ``https://e-jagriti.gov.in/services`` are public plain
JSON. This session does GET/POST, sends browser-like headers defensively,
unwraps the ``{data, message, error, status}`` envelope (note: ``error`` is a
STRING ``"false"``/``"true"``), and classifies transport failures into the
shared taxonomy (``CourtSiteDown``/``RateLimited``/``BlockedByGeoIP``) so the
resilience stack + callers behave identically to eCourts.

Retry-free by design — the outer ``with_retry`` / per-forum circuit breaker owns
retries (mirrors ``_session`` in the eCourts client). See
``docs/spike-ejagriti-transport.md`` for the endpoint map + provenance.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import requests

from ecourts_client.errors import (
    BlockedByGeoIP,
    CourtSiteDown,
    ECourtsError,
    PDFInvalid,
    PDFNotFound,
    RateLimited,
    SchemaChanged,
)

BASE_URL = "https://e-jagriti.gov.in/services"
_ORIGIN = "https://e-jagriti.gov.in"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_TIMEOUT = 30


@dataclass
class ConsumerSession:
    """One HTTP session against the e-Jagriti public JSON API."""

    base_url: str = BASE_URL
    _http: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = requests.Session()
        # Defensive browser-like headers: e-Jagriti does NOT enforce Origin/
        # Referer today (bare curl gets 200), but sending them is cheap insurance
        # against a future WAF that keys on them.
        self._http.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Origin": _ORIGIN,
                "Referer": f"{_ORIGIN}/",
                "Accept": "application/json, text/plain, */*",
            }
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._call("GET", path, params=params)

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return self._call("POST", path, params=params, json_body=body)

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Perform the call, classify failures, unwrap the envelope, return ``data``."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            resp = self._http.request(
                method, url, params=params, json=json_body, timeout=_TIMEOUT
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            raise CourtSiteDown(f"connection error on {path}: {e}") from e

        if resp.status_code == 429:
            raise RateLimited(f"e-Jagriti returned 429 for {path}")
        if resp.status_code == 403:
            # GeoIP block is non-retryable; a bare 403 is usually a WAF/transient
            # gate → treat as CourtSiteDown (retryable) like the eCourts client.
            if "geo" in resp.text.lower():
                raise BlockedByGeoIP(f"GeoIP block for {path}")
            raise CourtSiteDown(f"403 (WAF/transient) on {path}")
        if 500 <= resp.status_code < 600:
            raise CourtSiteDown(f"{resp.status_code} on {path}")
        if resp.status_code == 401:
            # The public tracking endpoints never 401; a 401 means we hit an
            # auth-gated sibling (getCaseHistory* / caseCategory / the filing
            # side) or NIC locked the endpoint. Hard error, not retryable.
            raise ECourtsError(
                f"401 auth-gated endpoint {path!r} — not a public tracking endpoint"
            )

        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            # A 200 with a non-JSON body is a WAF/maintenance HTML interstitial,
            # not a domain response — transient/retryable.
            raise CourtSiteDown(f"non-JSON 200 from {path}: {resp.text[:120]!r}")

        # Some listers may return a bare top-level array — that IS the data.
        if isinstance(body, list):
            return body
        if not isinstance(body, dict):
            raise SchemaChanged("body", f"unexpected {type(body).__name__} payload from {path}")

        # Envelope: {data, message, error:"false"/"true" (STRING!), status}.
        # The string `error` is authoritative; `status` is a heuristic — coerce
        # it (may be a numeric string) and map transport-ish codes into the
        # shared taxonomy so backoff/breaker behave.
        err = body.get("error")
        status_raw = body.get("status")
        try:
            status_code = int(str(status_raw)) if status_raw is not None else None
        except (TypeError, ValueError):
            status_code = None
        if status_code == 429:
            raise RateLimited(f"{path}: app-status 429")
        if status_code is not None and status_code >= 500:
            raise CourtSiteDown(f"{path}: app-status {status_code}")
        if (isinstance(err, str) and err.strip().lower() == "true") or (
            status_code is not None and status_code >= 400
        ):
            raise ECourtsError(
                f"{path}: {body.get('message') or 'e-Jagriti error'} (status={status_raw})"
            )
        return body.get("data")

    def fetch_pdf(self, url: str) -> bytes:
        """GET an order/judgment PDF by (absolute or base-relative) path.

        NOTE: e-Jagriti often returns order PDFs *inline* as base64 on the case
        row (handled by the caller off the ``Case``); this path-based fetch
        covers the ``orderDocumentPath`` case. The exact PDF-URL field is a
        spike open item pending a live populated-row capture — validate here by
        the ``%PDF`` magic so a wrong path fails loud instead of storing HTML.
        """
        full = url if url.startswith("http") else f"{self.base_url}/{url.lstrip('/')}"
        try:
            resp = self._http.get(full, timeout=_TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise CourtSiteDown(f"connection error fetching PDF {full}: {e}") from e
        if resp.status_code == 404:
            raise PDFNotFound(f"404 for PDF {full}")
        if 500 <= resp.status_code < 600:
            raise CourtSiteDown(f"{resp.status_code} fetching PDF {full}")
        content = resp.content
        # NIC/nginx servers sometimes prepend CRLF/BOM before %PDF- (see the
        # eCourts pdf.py); scan the first 1KB rather than requiring it at offset 0.
        if b"%PDF" not in content[:1024]:
            raise PDFInvalid(f"non-PDF body from {full} (first bytes: {content[:16]!r})")
        return content
