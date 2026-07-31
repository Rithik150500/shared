"""Maps an exception to what it says about upstream availability.

Pre-existing defect this fixes: ``circuit_breaker.py`` counts EVERY exception
(``except Exception: cb.record_failure()`` at :176-178 and :202-204), so
client-side and content errors open a breaker that is supposed to track
*upstream* health. Five user-typed bad CNRs are enough to reach
``failure_threshold=5`` and open the process-wide breaker for every tenant.

Scope note: this module ships ahead of per-court breaker keying. Today both
``TRIP_GLOBAL`` and ``TRIP_COURT`` are recorded against the single
``ecourts_global`` breaker; the distinction is carried now because it encodes a
reviewed judgement about which failures are IP-wide and which are court-local,
and it is what per-court keying will route on later.
"""
from __future__ import annotations

import enum

from ecourts_client.errors import (
    BlockedByGeoIP,
    CircuitOpen,
    CNRMalformed,
    CNRNotFound,
    CourtSiteDown,
    ECourtsError,
    ForumNotAutomated,
    IdentifierMalformed,
    JWTExpired,
    PDFInvalid,
    PDFNotFound,
    PDFRequestRejected,
    RateLimited,
    SchemaChanged,
)


class Outcome(enum.Enum):
    """What an exception says about upstream health."""

    TRIP_GLOBAL = "trip_global"   # IP-wide: affects every court at once
    TRIP_COURT = "trip_court"     # court-local availability failure
    NEUTRAL = "neutral"           # says nothing about availability -- do not count


# ORDER IS LOAD-BEARING -- the first isinstance match wins:
#   1. CircuitOpen first. It is an ECourtsError raised BY the breaker; counting
#      it would make an open breaker record a failure on every rejected call and
#      never recover.
#   2. Concrete types next.
#   3. The ECourtsError base LAST -- it is the parent of everything above.
_SPECIFIC: tuple[tuple[type[BaseException], Outcome], ...] = (
    (CircuitOpen, Outcome.NEUTRAL),
    # -- IP-wide: a property of the egress IP, not of any one court.
    (RateLimited, Outcome.TRIP_GLOBAL),
    (BlockedByGeoIP, Outcome.TRIP_GLOBAL),
    # -- court-local availability.
    (CourtSiteDown, Outcome.TRIP_COURT),
    # -- client-side facts: no wire call, or a coherent negative answer.
    (CNRNotFound, Outcome.NEUTRAL),
    (CNRMalformed, Outcome.NEUTRAL),
    (IdentifierMalformed, Outcome.NEUTRAL),
    (ForumNotAutomated, Outcome.NEUTRAL),
    # -- content facts: the host answered, the payload disappointed us.
    (PDFNotFound, Outcome.NEUTRAL),
    (PDFInvalid, Outcome.NEUTRAL),
    # -- our bug, not theirs: eCourts answered 200 and told us the request was
    #    malformed. Charging it to the court's availability opened breakers in
    #    front of orders that download fine (hc:DL, 2026-07-31). Alert, retry
    #    never, back off never.
    (PDFRequestRejected, Outcome.NEUTRAL),
    # -- our bug or NIC drift. Opening a breaker on a code defect manufactures
    #    a fake outage; alert on these instead of backing off.
    (SchemaChanged, Outcome.NEUTRAL),
    # -- remedy is a re-mint, not a backoff. An open circuit would suppress
    #    exactly the calls that warm a fresh session.
    (JWTExpired, Outcome.NEUTRAL),
)

# Error types that only exist on some pins of this package. Imported defensively
# so the policy stays portable: casepilot and ecourts-bot pin DIFFERENT
# ecourts_client SHAs, and the Supreme Court adapter is absent from older ones.
_OPTIONAL: tuple[tuple[type[BaseException], Outcome], ...] = ()
try:  # pragma: no cover - depends on the pin
    from ecourts_client.supreme._session import SCTokenInvalid, SCTokenMissing
except Exception:  # noqa: BLE001 - any import failure means "not on this pin"
    pass
else:
    _OPTIONAL = (
        # Credential/config faults, not upstream availability. The remedy is
        # re-capturing SC_MOBILE_TOKEN; backing off would only suppress the
        # calls that would validate a refreshed token.
        (SCTokenMissing, Outcome.NEUTRAL),
        (SCTokenInvalid, Outcome.NEUTRAL),
    )

# The ECourtsError base sits LAST -- it is the parent of everything above. Bare
# ECourtsError is still raised in _session.py and consumer/_session.py; until
# those are retyped, trip conservatively rather than silently losing coverage.
_POLICY: tuple[tuple[type[BaseException], Outcome], ...] = (
    *_SPECIFIC,
    *_OPTIONAL,
    (ECourtsError, Outcome.TRIP_GLOBAL),
)

#: Every type given an explicit decision above. The exhaustiveness meta-test in
#: tests/unit/test_failure_policy.py asserts nothing is classified by accident.
KNOWN_TYPES: tuple[type[BaseException], ...] = tuple(t for t, _ in _POLICY)

#: Anything that is not an ECourtsError at all -- a leaked third-party
#: exception. Treated as a real failure, with the narrower blast radius.
_FAILSAFE = Outcome.TRIP_COURT


def classify_failure(exc: BaseException) -> Outcome:
    """Classify ``exc`` as an availability signal (or not)."""
    for exc_type, outcome in _POLICY:
        if isinstance(exc, exc_type):
            return outcome
    return _FAILSAFE
