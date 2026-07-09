from __future__ import annotations

import requests

import ecourts_client._session as s
from ecourts_client._session import Session, _penalize_rate_gate
from ecourts_client.errors import RateLimited


class _RecordingLimiter:
    """Stand-in for RedisRateLimiter capturing penalize() calls."""
    def __init__(self): self.penalized = 0
    def wait(self): pass
    def penalize(self): self.penalized += 1


class _Canned:
    def __init__(self, code, text): self.status_code = code; self._text = text; self.headers = {}
    def get(self, *a, **k):
        r = requests.Response(); r.status_code = self.status_code; r._content = self._text.encode(); return r


def test_penalize_helper_noops_on_plain_rate_gate(monkeypatch):
    from ecourts_client._session import _RateGate
    monkeypatch.setattr(s, "_rate_gate", _RateGate(0.0))
    _penalize_rate_gate()  # must not raise


def test_405_penalizes_the_redis_limiter(monkeypatch):
    lim = _RecordingLimiter()
    monkeypatch.setattr(s, "_rate_gate", lim)
    sess = Session(scope="district"); sess.jwt = "x"
    sess._http = _Canned(405, "<html>Search Page not Found here</html>")
    import pytest
    with pytest.raises(RateLimited):
        sess._send("searchByPartyName.php", {}, with_bearer=True)
    assert lim.penalized == 1, "a 405 must widen the shared limiter interval"
