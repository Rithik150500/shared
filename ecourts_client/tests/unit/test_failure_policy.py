"""Failure taxonomy -- what an exception says about upstream availability.

Pre-existing defect this addresses: both circuit-breaker wrappers
(circuit_breaker.py:176-178 async, :202-204 sync) do a bare
``except Exception: cb.record_failure()``, so EVERY exception counts as an
availability signal -- including client-side and content errors. Five
user-typed bad CNRs therefore reach failure_threshold=5 and open the
process-wide breaker for every tenant.
"""
from __future__ import annotations

import pytest

from ecourts_client import errors as E
from ecourts_client.resilience.failure_policy import (
    KNOWN_TYPES,
    Outcome,
    classify_failure,
)


def _all_ecourts_error_types() -> set[type]:
    """Every ECourtsError type, including the base class itself."""
    seen: set[type] = {E.ECourtsError}
    stack = [E.ECourtsError]
    while stack:
        for sub in stack.pop().__subclasses__():
            if sub not in seen:
                seen.add(sub)
                stack.append(sub)
    return seen


def test_cnr_not_found_does_not_signal_an_outage():
    """The headline regression: a bad CNR is a client fact, not a court outage.

    Reached only after the host returned a well-formed decrypted response with
    an empty history -- positive evidence the court is up.
    """
    assert classify_failure(E.CNRNotFound("MHAU019999992015")) is Outcome.NEUTRAL


@pytest.mark.parametrize(
    "exc",
    [
        E.CNRMalformed("nope"),
        E.IdentifierMalformed("consumer", "x"),
        E.ForumNotAutomated("arbitration"),
        E.PDFNotFound("404"),
        E.PDFInvalid("bad magic"),
        E.SchemaChanged(field="caseNumber", reason="missing"),
        E.JWTExpired("two 401s"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_client_and_content_errors_are_neutral(exc):
    assert classify_failure(exc) is Outcome.NEUTRAL


def test_circuit_open_is_neutral_so_an_open_breaker_cannot_feed_itself():
    """CircuitOpen is an ECourtsError raised BY the breaker.

    Counting it would make an open breaker record a failure on every rejected
    call and never recover. Must be matched before any base-class arm.
    """
    exc = E.CircuitOpen(name="ecourts_global", retry_after_seconds=1.0)
    assert classify_failure(exc) is Outcome.NEUTRAL


@pytest.mark.parametrize(
    "exc", [E.RateLimited("429"), E.BlockedByGeoIP("403 geo")],
    ids=lambda e: type(e).__name__,
)
def test_ip_wide_errors_trip_global(exc):
    """Properties of the egress IP -- they affect every court at once."""
    assert classify_failure(exc) is Outcome.TRIP_GLOBAL


def test_court_site_down_trips_court():
    """HTTP 5xx from the court payload -- the genuinely court-scoped signal."""
    assert classify_failure(E.CourtSiteDown("502")) is Outcome.TRIP_COURT


def test_unclassified_ecourts_error_trips_conservatively():
    """Bare ECourtsError is still raised in _session.py / consumer/_session.py.

    Until those raise sites are retyped, preserve today's protective behaviour
    rather than silently going neutral and losing breaker coverage.
    """
    assert classify_failure(E.ECourtsError("bare")) is Outcome.TRIP_GLOBAL


def test_non_ecourts_exception_trips_as_failsafe():
    """A leaked third-party exception is treated as an availability failure."""
    assert classify_failure(RuntimeError("leaked from requests")) is Outcome.TRIP_COURT


def test_supreme_court_token_errors_are_neutral():
    """SC token faults are credential/config problems, not court outages.

    The remedy is re-capturing SC_MOBILE_TOKEN; an open circuit would suppress
    exactly the calls that would validate a refreshed token. Skipped on pins
    that predate the Supreme Court adapter.
    """
    sc = pytest.importorskip("ecourts_client.supreme._session")

    assert classify_failure(sc.SCTokenMissing("no token")) is Outcome.NEUTRAL
    assert classify_failure(sc.SCTokenInvalid("rejected")) is Outcome.NEUTRAL


def test_session_jwt_expired_is_the_publicly_exported_type():
    """Regression: there were two unrelated JWTExpired classes.

    ``_session.py:194`` defined its own ``JWTExpired`` and ``:263`` raised it,
    while ``__init__.py:23`` exported the *other* one from ``errors.py:67``.
    So ``except ecourts_client.JWTExpired:`` could never catch the only
    JWTExpired ever raised, and the taxonomy would classify the wrong type.
    """
    from ecourts_client import _session

    assert _session.JWTExpired is E.JWTExpired


def test_every_ecourts_error_type_is_explicitly_classified():
    """No error type may reach a classification by accident.

    Walks the base class INCLUDING itself -- a walk of only __subclasses__()
    would be blind to precisely the gap it is meant to guard.
    """
    missing = _all_ecourts_error_types() - set(KNOWN_TYPES)
    assert not missing, (
        "unclassified error types: " + ", ".join(sorted(t.__name__ for t in missing))
    )
