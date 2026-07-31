from __future__ import annotations

import re
from typing import Literal

from ecourts_client.errors import CNRMalformed, IdentifierMalformed
from ecourts_client.forums import (
    FORUM_IDENTIFIER_KIND,
    Forum,
    IdentifierKind,
)


CnrScope = Literal["district", "highcourt"]

CNR_REGEX = re.compile(r"^[A-Z]{2}[A-Z0-9]{14}$")

# State codes per NIC's eCourts taxonomy. Full list per spec; abbreviated here.
STATE_CODES: dict[str, str] = {
    "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh", "AS": "Assam", "BR": "Bihar",
    "CG": "Chhattisgarh", "DL": "Delhi", "GA": "Goa", "GJ": "Gujarat", "HR": "Haryana",
    "HP": "Himachal Pradesh", "JH": "Jharkhand", "KA": "Karnataka", "KL": "Kerala",
    "MP": "Madhya Pradesh", "MH": "Maharashtra", "MN": "Manipur", "ML": "Meghalaya",
    "MZ": "Mizoram", "NL": "Nagaland", "OR": "Odisha", "PB": "Punjab", "RJ": "Rajasthan",
    "SK": "Sikkim", "TN": "Tamil Nadu", "TG": "Telangana", "TR": "Tripura",
    "UP": "Uttar Pradesh", "UK": "Uttarakhand", "WB": "West Bengal", "JK": "Jammu and Kashmir",
    "LA": "Ladakh", "AN": "Andaman and Nicobar Islands", "CH": "Chandigarh",
    "DD": "Dadra and Nagar Haveli and Daman and Diu", "LD": "Lakshadweep", "PY": "Puducherry",
}

HC_ESTABLISHMENT_CODES: set[str] = {"HC"}  # 3rd-4th chars when scope is HC

# High Courts whose establishment code is NOT the literal 'HC'. These MUST be
# matched as a (state, establishment) PAIR, never on the establishment alone:
# ('WB','CH') is the Calcutta High Court, but ('CH','CH') is the Chandigarh
# DISTRICT court, so adding 'CH' to HC_ESTABLISHMENT_CODES would misroute every
# Chandigarh district case to the High Court client.
#
# A misroute here is SILENT and presents as a court outage: the district client
# calls listOfCasesWebService.php (district-only), gets "Record not found", and
# the whole add/poll dies with "Failed to process case" even though the case
# fetches perfectly from the HC portal.
HC_STATE_ESTABLISHMENT_PAIRS: set[tuple[str, str]] = {
    ("WB", "CH"),  # Calcutta High Court, e.g. WBCHCA.../WBCHCO...
}


def validate_cnr_shape(cnr: str) -> None:
    """Raise CNRMalformed if shape or state code is invalid.

    The CNR format is 16 chars: [STATE-2 letters][ESTABLISHMENT-14 alnum].
    The establishment segment is alphanumeric and MAY START WITH DIGITS --
    e.g. Madhya Pradesh district CNRs like 'MP20060042872025' (chars 2:4 =
    '20'), and some Punjab benches encode special info as a letter
    ('PBASB10004672023'). So chars 2:4 are NOT necessarily letters; the older
    ``[A-Z]{2}[A-Z]{2}...`` shape wrongly rejected every state whose
    establishment code begins numerically. Note: state-code 'GA' is also
    reused for Gauhati HC (jurisdiction over Assam/Meghalaya/etc), not just
    Goa, so we don't disambiguate scope by state alone -- the court-type code
    (chars 2:4) is the source of truth and equals the literal 'HC' for High
    Court CNRs (digits never collide with 'HC').
    """
    if not isinstance(cnr, str) or not CNR_REGEX.match(cnr):
        raise CNRMalformed(cnr=cnr, reason="failed regex [A-Z]{2}[A-Z0-9]{14}")
    state = cnr[:2]
    court_type = cnr[2:4]
    # High Court CNRs use TWO eCourts conventions:
    #   1. [STATE][HC] -- the 'HC' establishment code sits in chars 2:4 with a
    #      real (or HC-specific, e.g. 'PH'/'GA') 2-letter code in the state slot.
    #   2. [HC][bench]  -- the literal 'HC' sits in the STATE slot (chars 0:2)
    #      and the bench/court code in chars 2:4, e.g. 'HCBM...' (Bombay),
    #      'HCMA...' (Madras). These are REAL CNRs the HC portal returns.
    # No geographic state is ever 'HC', so an 'HC' state slot is unambiguously a
    # High Court. Only enforce the geographic-state whitelist for District Courts.
    if (
        state != "HC"
        and court_type not in HC_ESTABLISHMENT_CODES
        and state not in STATE_CODES
    ):
        raise CNRMalformed(cnr=cnr, reason=f"unknown state code '{state}'")


def classify_cnr(cnr: str) -> CnrScope:
    """Return 'district' or 'highcourt'.

    High Court when the 'HC' establishment code is in chars 2:4 ([STATE][HC],
    e.g. 'KAHC'), or 'HC' is in the state slot ([HC][bench], e.g. 'HCBM' Bombay
    / 'HCMA' Madras), or the (state, establishment) pair is a known High Court
    that does not use the literal 'HC' code (see HC_STATE_ESTABLISHMENT_PAIRS —
    currently Calcutta, 'WBCH'). Everything else is District Court.
    """
    validate_cnr_shape(cnr)
    state, court_type = cnr[:2], cnr[2:4]
    if (
        court_type in HC_ESTABLISHMENT_CODES
        or state == "HC"
        or (state, court_type) in HC_STATE_ESTABLISHMENT_PAIRS
    ):
        return "highcourt"
    return "district"


def forum_for_cnr(cnr: str) -> Forum:
    """Map a CNR to its eCourts Forum (ecourts_district / ecourts_highcourt).

    Back-compat bridge from the CNR-first world to the forum-first one: the
    legacy ``fetch_case(cnr)`` path stays on ``classify_cnr``; this lets the new
    forum-aware layer derive the canonical Forum for an eCourts CNR.
    """
    return (
        Forum.ECOURTS_HIGHCOURT
        if classify_cnr(cnr) == "highcourt"
        else Forum.ECOURTS_DISTRICT
    )


def validate_identifier(forum: Forum, identifier: str) -> None:
    """Validate an identifier against its forum's expected kind.

    eCourts forums delegate to ``validate_cnr_shape`` (raising ``CNRMalformed``),
    preserving the exact legacy CNR safety. Manual forums (arbitration) accept
    any opaque ref. Other automated forums get a light non-empty check here;
    their per-forum adapters do the stricter, format-specific validation.
    """
    kind = FORUM_IDENTIFIER_KIND[forum]
    if kind is IdentifierKind.CNR:
        validate_cnr_shape(identifier)
        return
    if kind is IdentifierKind.MANUAL:
        return
    if not isinstance(identifier, str) or not identifier.strip():
        raise IdentifierMalformed(
            forum=forum.value, identifier=identifier, reason="empty identifier"
        )
