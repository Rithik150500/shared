"""Derive a stable circuit-breaker key from a CNR.

A key becomes a PERMANENT entry in the process-wide breaker registry (there is
deliberately no eviction -- see ``circuit_breaker._CircuitRegistry`` -- because
evicting would silently reset a legitimately-open breaker mid-outage). It is
also a natural metric label. So this module must be **total** (never raise) and
must never echo caller-supplied text back as a key: every component is either a
fixed prefix or exactly two characters validated against a closed set.

Granularity is state / bench-family, not establishment. Containment is the goal
-- "Delhi HC is down must not block Rajasthan district" is a failure mode we
actually see; "Jodhpur bench down must not block Jaipur bench" is not. Coarser
keys also keep cardinality enumerable (~36 district states + the HC families).
"""
from __future__ import annotations

import re

from ecourts_client.routing import CNR_REGEX, HC_ESTABLISHMENT_CODES, STATE_CODES

#: IP-wide breaker. Name unchanged so existing dashboards/tests keep working.
GLOBAL_KEY = "ecourts_global"

#: Court could not be derived. A single shared bucket by design -- it must not
#: become a per-input namespace.
UNKNOWN_KEY = "ecourts_unknown"

_DISTRICT_PREFIX = "dc"
_HIGHCOURT_PREFIX = "hc"

#: Court-scoped keys look like ``dc:MH`` / ``hc:BM``.
_COURT_KEY_RE = re.compile(r"^(dc|hc):[A-Z]{2}$")

#: The HC state/bench slot must be two LETTERS. Bounds the HC key space and
#: rejects digit slots, which are never a real bench code.
_HC_SLOT_RE = re.compile(r"^[A-Z]{2}$")


def is_court_scoped(name: str) -> bool:
    """True for per-court breaker names, False for the global/forum ones."""
    return bool(_COURT_KEY_RE.match(name)) if isinstance(name, str) else False


def court_key_for_cnr(cnr: object) -> str:
    """Map a CNR to a stable court key, or ``UNKNOWN_KEY``.

    Never raises. Mirrors ``routing.validate_cnr_shape`` on the three real CNR
    shapes:

    * ``[HC][bench]``  -- literal 'HC' in the state slot, e.g. HCBM / HCMA.
    * ``[STATE][HC]``  -- 'HC' in chars 2:4, e.g. DLHC, PHHC.
    * ``[STATE][...]`` -- district; the establishment segment may be numeric
      (e.g. MP2006...), so only chars 0:2 are meaningful here.
    """
    if not isinstance(cnr, str) or not CNR_REGEX.match(cnr):
        return UNKNOWN_KEY

    state_slot = cnr[:2]
    court_type = cnr[2:4]

    # Form 2: literal 'HC' in the STATE slot; the bench code follows.
    if state_slot == "HC":
        return f"{_HIGHCOURT_PREFIX}:{court_type}" if _HC_SLOT_RE.match(court_type) else UNKNOWN_KEY

    # Form 1: 'HC' establishment code in chars 2:4; the state slot names the HC.
    if court_type in HC_ESTABLISHMENT_CODES:
        return f"{_HIGHCOURT_PREFIX}:{state_slot}" if _HC_SLOT_RE.match(state_slot) else UNKNOWN_KEY

    # District: the state slot must be a real geographic state. An unrecognised
    # code is NOT given its own key -- otherwise junk input mints registry
    # entries and metric labels without bound.
    if state_slot in STATE_CODES:
        return f"{_DISTRICT_PREFIX}:{state_slot}"

    return UNKNOWN_KEY
