"""A shared warm Session is minted/used concurrently (asyncio.to_thread pool +
casepilot concurrent ingest). Pins: concurrent cold-start -> ONE mint, concurrent
401 -> ONE re-mint, the (previously UNTESTED) single/double-401 contract, and the
shared-mutation invariant (no lock-free jwt writer on data responses).
"""
from __future__ import annotations

import threading
import time

import pytest

from ecourts_client._session import JWTExpired, Session


def _fresh() -> Session:
    return Session(scope="district")


def test_jwtexpired_is_unified_with_errors_module():
    """De-dup guard: _session no longer defines its own JWTExpired class; it
    re-exports ecourts_client.errors.JWTExpired, so a caller doing
    `except errors.JWTExpired` catches what Session.call actually raises."""
    from ecourts_client import _session, errors

    assert _session.JWTExpired is errors.JWTExpired


def test_concurrent_cold_callers_mint_exactly_once(monkeypatch):
    s = _fresh()
    mints = []

    def counting_init(self, **_kw):   # **_kw: init() takes use_shared=
        mints.append(1)
        time.sleep(0.02)          # widen the race window
        self.jwt = "tok"
        self._mint_gen += 1

    monkeypatch.setattr(Session, "init", counting_init)
    threads = [threading.Thread(target=s._ensure_jwt) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(mints) == 1, f"expected 1 mint across 50 threads, got {len(mints)}"
    assert s.jwt == "tok"


def test_concurrent_401s_reminted_once(monkeypatch):
    s = _fresh()
    s.jwt = "stale"
    s._mint_gen = 1
    mints = []

    def counting_init(self, **_kw):   # **_kw: init() takes use_shared=
        mints.append(1)
        time.sleep(0.02)
        self.jwt = "fresh"
        self._mint_gen += 1

    monkeypatch.setattr(Session, "init", counting_init)
    threads = [threading.Thread(target=s._remint_on_401, args=(1,)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(mints) == 1, f"expected 1 re-mint across 20 concurrent 401s, got {len(mints)}"
    assert s.jwt == "fresh"


def test_remint_skipped_when_generation_already_advanced(monkeypatch):
    s = _fresh()
    s.jwt = "fresh"
    s._mint_gen = 5  # someone already re-minted since the caller's gen (=4)
    monkeypatch.setattr(Session, "init", lambda self, **_kw: pytest.fail("must not re-mint"))
    s._remint_on_401(gen_used=4)
    assert s.jwt == "fresh"


def test_single_401_re_mints_once_then_succeeds(monkeypatch):
    """CONTRACT (previously untested): one 401 -> exactly one extra mint + retry -> good result."""
    s = _fresh()
    mints = {"n": 0}
    data_calls = {"n": 0}

    def fake_send(self, endpoint, payload, *, with_bearer, method="GET"):
        if endpoint == "appReleaseWebService.php":
            mints["n"] += 1
            self.jwt = f"tok{mints['n']}"      # init() sets jwt from the returned body
            self._mint_gen += 1
            return {"token": self.jwt}
        data_calls["n"] += 1
        if data_calls["n"] == 1:
            return {"status": "N", "status_code": "401"}   # first attempt: expired
        return {"status": "Y", "ok": True}

    monkeypatch.setattr(Session, "_send", fake_send)
    result = s.call("caseHistoryWebService.php", {})
    assert result == {"status": "Y", "ok": True}
    assert mints["n"] == 2, f"cold mint + one re-mint expected, got {mints['n']}"
    assert data_calls["n"] == 2


def test_double_401_raises_jwt_expired(monkeypatch):
    s = _fresh()

    def fake_send(self, endpoint, payload, *, with_bearer, method="GET"):
        if endpoint == "appReleaseWebService.php":
            self.jwt = "tok"
            self._mint_gen += 1
            return {"token": "tok"}
        return {"status": "N", "status_code": "401"}   # always 401

    monkeypatch.setattr(Session, "_send", fake_send)
    with pytest.raises(JWTExpired):
        s.call("caseHistoryWebService.php", {})


def test_double_401_does_not_null_jwt_lock_free(monkeypatch):
    """Invariant guard: after a double-401 JWTExpired, self.jwt is NOT nulled
    lock-free (a concurrent _send must never see None mid-flight)."""
    s = _fresh()
    s.jwt = "tok"
    s._mint_gen = 1

    def fake_send(self, endpoint, payload, *, with_bearer, method="GET"):
        if endpoint == "appReleaseWebService.php":   # re-mint must succeed to reach the 2nd 401
            self.jwt = "tok2"
            self._mint_gen += 1
            return {"token": "tok2"}
        return {"status": "N", "status_code": "401"}   # data endpoint always 401

    monkeypatch.setattr(Session, "_send", fake_send)
    with pytest.raises(JWTExpired):
        s.call("caseHistoryWebService.php", {})
    assert s.jwt is not None, "double-401 must not null jwt lock-free (shared-Session race)"
