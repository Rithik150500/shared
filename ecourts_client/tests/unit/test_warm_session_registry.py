from __future__ import annotations

from ecourts_client._session import Session, get_warm_session, reset_warm_sessions


def test_same_scope_returns_same_instance():
    reset_warm_sessions()
    assert get_warm_session("district") is get_warm_session("district")


def test_distinct_scopes_are_isolated():
    reset_warm_sessions()
    dc, hc = get_warm_session("district"), get_warm_session("highcourt")
    assert dc is not hc
    assert dc.scope == "district" and hc.scope == "highcourt"


def test_reset_clears_the_registry():
    reset_warm_sessions()
    first = get_warm_session("district")
    reset_warm_sessions()
    assert get_warm_session("district") is not first


def test_warm_session_reuses_jwt_across_calls(monkeypatch):
    """Core regression guard: N calls through the warm Session mint ONCE."""
    reset_warm_sessions()  # belt-and-suspenders (do not rely only on autouse ordering)
    mints = {"n": 0}

    def fake_send(self, endpoint, payload, *, with_bearer, method="GET"):
        if endpoint == "appReleaseWebService.php":
            mints["n"] += 1
            self.jwt = "tok"
            self._mint_gen += 1
            return {"token": "tok"}
        return {"status": "Y", "ok": True}

    monkeypatch.setattr(Session, "_send", fake_send)
    s = get_warm_session("district")
    s.call("listOfCasesWebService.php", {})
    s.call("caseHistoryWebService.php", {})
    s.call("caseHistoryWebService.php", {})
    assert mints["n"] == 1, f"warm session should mint once, minted {mints['n']}x"
