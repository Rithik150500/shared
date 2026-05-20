"""A-5 audit fix: retry budget is owned by the outer ``with_retry`` decorator only.

Before A-5, ``_session.Session._send`` had its own ``range(_MAX_5XX_RETRIES + 1)``
inner loop with exponential backoff. Composed with the outer ``with_retry`` (which
also retries ``CourtSiteDown``), a sticky 5xx produced ``(inner+1) * outer`` HTTP
hits -- up to 12 attempts per call and several minutes of stall under a court-site
brownout, which then starved the bot-session pool.

These tests pin the post-fix contract:

  * A sticky 5xx in ``_send`` raises ``CourtSiteDown`` on the *first* HTTP call --
    no inner retries.
  * When ``Session.call`` is wrapped by ``with_retry(max_attempts=N)``, exactly N
    HTTP requests hit the wire (the outer layer owns retries end-to-end).
"""
from __future__ import annotations

import asyncio

import pytest
import requests

from ecourts_client._session import Session
from ecourts_client.errors import CourtSiteDown
from ecourts_client.resilience.retry import with_retry


class _Sticky5xxHTTP:
    """Stand-in for ``requests.Session`` that always returns 503.

    Tracks every ``.get`` call so the test can assert the total HTTP
    fan-out across whatever retry policy is in effect.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.headers: dict[str, str] = {}

    def get(self, *args, **kwargs):
        self.calls += 1
        resp = requests.Response()
        resp.status_code = 503
        resp._content = b"<html>maintenance</html>"
        return resp


def _make_session_with_jwt() -> Session:
    """A Session with JWT already set so ``call`` skips bootstrap and goes
    straight to ``_send``."""
    s = Session(scope="district")
    s.jwt = "fake-jwt-for-test"
    return s


def test_send_does_not_retry_5xx_internally():
    """``_send`` raises CourtSiteDown on the first 5xx -- inner loop removed."""
    s = _make_session_with_jwt()
    fake = _Sticky5xxHTTP()
    s._http = fake  # type: ignore[assignment]

    with pytest.raises(CourtSiteDown):
        s._send("stateWebService.php", {}, with_bearer=True)

    assert fake.calls == 1, (
        f"_send made {fake.calls} HTTP requests; A-5 expects 1 (outer retry owns the budget)"
    )


def test_outer_retry_is_the_only_retry_layer():
    """End-to-end: a sticky 5xx under outer ``with_retry(max_attempts=N)`` produces
    exactly N HTTP requests -- not N x (inner+1).

    Pre-fix, with ``_MAX_5XX_RETRIES=3`` inside ``_send`` and ``max_attempts=3``
    outside, this would have been 12 HTTP calls. Post-fix it is 3.
    """
    s = _make_session_with_jwt()
    fake = _Sticky5xxHTTP()
    s._http = fake  # type: ignore[assignment]

    max_attempts = 3

    @with_retry(max_attempts=max_attempts, base_delay=0.005)
    async def call_async():
        # Synchronous call dispatched directly -- mirrors the production
        # ``client._fetch_case_async`` -> ``asyncio.to_thread(...)`` shape but
        # we run it inline because the fake transport has no real I/O.
        return s.call("stateWebService.php", {})

    with pytest.raises(CourtSiteDown):
        asyncio.run(call_async())

    assert fake.calls == max_attempts, (
        f"sticky 5xx produced {fake.calls} HTTP requests under max_attempts={max_attempts}; "
        "outer retry must be the sole retry layer"
    )


def test_send_does_not_retry_connection_error_internally():
    """Network errors (ConnectionError / Timeout) also surface immediately as
    CourtSiteDown -- the outer retry decides whether to re-try."""
    s = _make_session_with_jwt()

    class _ConnRefusedHTTP:
        def __init__(self) -> None:
            self.calls = 0
            self.headers: dict[str, str] = {}

        def get(self, *args, **kwargs):
            self.calls += 1
            raise requests.ConnectionError("no route to host")

    fake = _ConnRefusedHTTP()
    s._http = fake  # type: ignore[assignment]

    with pytest.raises(CourtSiteDown):
        s._send("stateWebService.php", {}, with_bearer=True)

    assert fake.calls == 1
