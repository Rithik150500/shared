from __future__ import annotations

import re
from typing import Literal

from ecourts_client.errors import CNRMalformed


CnrScope = Literal["district", "highcourt"]

CNR_REGEX = re.compile(r"^[A-Z]{2}[A-Z]{2}[A-Z0-9]{12}$")

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


def validate_cnr_shape(cnr: str) -> None:
    """Raise CNRMalformed if shape or state code is invalid.

    The CNR format is 16 chars: [STATE-2][COURT-2][ESTABLISHMENT-12]. The
    establishment segment is alphanumeric -- some Punjab benches encode special
    info as a letter in this segment (e.g. 'PBASB10004672023'). Note: state-code
    'GA' is also reused for Gauhati HC (jurisdiction over Assam/Meghalaya/etc),
    not just Goa, so we don't try to disambiguate scope by state alone -- court
    type code (chars 2:4) is the source of truth.
    """
    if not isinstance(cnr, str) or not CNR_REGEX.match(cnr):
        raise CNRMalformed(cnr=cnr, reason="failed regex [A-Z]{2}[A-Z]{2}[A-Z0-9]{12}")
    state = cnr[:2]
    if state not in STATE_CODES:
        raise CNRMalformed(cnr=cnr, reason=f"unknown state code '{state}'")


def classify_cnr(cnr: str) -> CnrScope:
    """Return 'district' or 'highcourt' based on CNR's 3rd-4th chars (court-type code)."""
    validate_cnr_shape(cnr)
    court_type = cnr[2:4]
    if court_type in HC_ESTABLISHMENT_CODES:
        return "highcourt"
    return "district"
