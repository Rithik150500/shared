"""Supreme Court (com.nic.sciapp) mobile-API session.

The SC Android app talks to a backend at ``https://scourtapp.sci.gov.in/``,
routed by ``?pageid=<code>&token=<T>``. Case-status (``pageid=030001``) returns
a server-rendered HTML case-detail page (parsed in ``parsers.py``).

⚠️ The ``token`` is a SESSION token minted by a mobile OTP login on a device —
it is NOT anonymous and NOT mintable server-side (see
``docs/RE_NOTES_sci.md``). Supply it via the ``SC_MOBILE_TOKEN`` env var; it
expires and must be periodically re-captured from the app. An invalid/expired
token makes the server return ``{"error":"Permission denyyy!"}`` → surfaced as
``SCTokenInvalid`` so callers can prompt a re-capture.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import requests

from ecourts_client.errors import CourtSiteDown, ECourtsError, RateLimited

BASE_URL = "https://scourtapp.sci.gov.in/"
TOKEN_ENV = "SC_MOBILE_TOKEN"
_UA = (
    "Mozilla/5.0 (Linux; Android 12; A001) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"
)
_TIMEOUT = 30


class SCTokenMissing(ECourtsError):
    """No SC_MOBILE_TOKEN configured."""


class SCTokenInvalid(ECourtsError):
    """The configured token was rejected ("Permission denyyy!") — re-capture."""


@dataclass
class SupremeSession:
    """One HTTP session against the SC mobile backend, GET + token."""

    base_url: str = BASE_URL
    token: str | None = None
    _http: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = requests.Session()
        self._http.headers.update(
            {"User-Agent": _UA, "Accept": "text/html,application/json,*/*"}
        )
        if not self.token:
            self.token = os.environ.get(TOKEN_ENV) or None

    def get(self, pageid: str, params: dict[str, Any] | None = None) -> str:
        """GET ``?pageid=<pageid>&token=<T>&...`` and return the response text.

        Raises SCTokenMissing (no token), SCTokenInvalid ("Permission denyyy!"),
        RateLimited (429) or CourtSiteDown (conn/5xx) — the shared taxonomy the
        resilience stack understands."""
        if not self.token:
            raise SCTokenMissing(
                f"no SC token — set {TOKEN_ENV} (capture from the SC app via adb/proxy)"
            )
        query = {"pageid": pageid, "token": self.token}
        if params:
            query.update(params)
        try:
            resp = self._http.get(self.base_url, params=query, timeout=_TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise CourtSiteDown(f"connection error on pageid={pageid}: {e}") from e
        if resp.status_code == 429:
            raise RateLimited(f"SC returned 429 for pageid={pageid}")
        if 500 <= resp.status_code < 600:
            raise CourtSiteDown(f"{resp.status_code} on pageid={pageid}")
        text = resp.text or ""
        if "Permission denyyy" in text:
            raise SCTokenInvalid(
                f"{TOKEN_ENV} invalid/expired — re-capture the token from the SC app"
            )
        return text
