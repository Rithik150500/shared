"""Throttle observability contract (audit finding: eCourts throttling was
invisible -- RateLimited/CourtSiteDown were raised silently, so throttle
frequency could not be measured).

These tests pin the instrumentation added at the ``_session._send`` classification
sites:
  * every throttle/failure classification emits ONE greppable WARNING carrying a
    stable ``ECOURTS_THROTTLE`` token + a ``kind=`` tag + the endpoint, so
    ``docker logs <c> | grep ECOURTS_THROTTLE`` yields a per-hour, per-kind tally
    across all containers without any new hard dependency; and
  * the best-effort Redis hour-bucket counter is FAIL-OPEN -- any Redis error (or
    redis not installed, as on casepilot) is a silent no-op that never masks the
    real RateLimited/CourtSiteDown the caller must see.
"""
from __future__ import annotations

import logging

import pytest
import requests

from ecourts_client import _session
from ecourts_client._session import Session
from ecourts_client.errors import CourtSiteDown, RateLimited


class _CannedHTTP:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self._text = text
        self.headers: dict[str, str] = {}

    def get(self, *args, **kwargs):
        resp = requests.Response()
        resp.status_code = self.status_code
        resp._content = self._text.encode("utf-8")
        return resp


def _sess(status_code: int, text: str) -> Session:
    s = Session(scope="district")
    s.jwt = "fake-jwt-for-test"  # skip bootstrap
    s._http = _CannedHTTP(status_code, text)  # type: ignore[assignment]
    return s


def test_405_emits_greppable_throttle_warning(caplog):
    s = _sess(405, "<html><body>Search Page not Found here</body></html>")
    with caplog.at_level(logging.WARNING, logger="ecourts_client._session"):
        with pytest.raises(RateLimited):
            s._send("searchByPartyName.php", {}, with_bearer=True)
    assert "ECOURTS_THROTTLE" in caplog.text
    assert "kind=throttle_405" in caplog.text
    assert "searchByPartyName.php" in caplog.text


def test_429_emits_greppable_throttle_warning(caplog):
    s = _sess(429, "rate limited")
    with caplog.at_level(logging.WARNING, logger="ecourts_client._session"):
        with pytest.raises(RateLimited):
            s._send("searchByPartyName.php", {}, with_bearer=True)
    assert "ECOURTS_THROTTLE" in caplog.text
    assert "kind=throttle_429" in caplog.text


def test_non_envelope_html_emits_throttle_warning(caplog):
    """A 200 HTML maintenance/edge-proxy page (a silent throttle vector) is tagged
    ``non_envelope`` so it is countable, not just an opaque CourtSiteDown."""
    s = _sess(200, "<html><body>site under maintenance</body></html>")
    with caplog.at_level(logging.WARNING, logger="ecourts_client._session"):
        with pytest.raises(CourtSiteDown):
            s._send("stateWebService.php", {}, with_bearer=True)
    assert "ECOURTS_THROTTLE" in caplog.text
    assert "kind=non_envelope" in caplog.text


def test_record_throttle_event_is_fail_open_when_redis_errors(monkeypatch):
    """The Redis counter must swallow ALL errors -- a broken/absent Redis must
    never surface from the classification path (casepilot has no redis wheel)."""
    def _boom():
        raise RuntimeError("redis down / not installed")

    monkeypatch.setattr(_session, "_throttle_redis_client", _boom)
    # Must not raise:
    _session._record_throttle_event("throttle_405", "searchByPartyName.php")


def test_throttle_classification_still_raises_when_counter_path_errors(monkeypatch):
    """Even if the counter path blows up, _send raises the real RateLimited."""
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(_session, "_throttle_redis_client", _boom)
    s = _sess(405, "<html>throttled</html>")
    with pytest.raises(RateLimited):
        s._send("searchByPartyName.php", {}, with_bearer=True)
