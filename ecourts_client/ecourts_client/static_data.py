"""Baked-in fallback for the eCourts state / High-Court picker lists.

WHY THIS EXISTS
    ``list_states()`` is the FIRST step of every guided search. The state list
    is quasi-immutable (numeric eCourts state codes change on a decade scale --
    a new Union Territory is a constitutional event), yet the live endpoint is
    exposed to the same IP-wide throttle a bulk case-load can trigger. When
    that throttle is active a user could not even *see* the list of states, so
    every search dead-ended at step 1 with "eCourts is rate-limiting."

    ``with_static_state_fallback`` (see ``_resilience_apply``) serves this
    snapshot whenever the live fetch errors or comes back empty, so the state
    step can never be throttled. The happy path still fetches live (and caches
    it), so a genuine future change is picked up automatically; the snapshot is
    only a floor.

PROVENANCE
    Captured 2026-07-16 from stateWebService.php (action_code=fillState) for
    both the district-court (36 states/UTs) and high-court (25 High Courts)
    scopes. The HC "state" step lists High Court NAMES, not geographic states,
    which is why the two scopes differ. To refresh: call
    ``DistrictCourtClient().list_states()`` / ``HighCourtClient().list_states()``
    and re-transcribe.
"""
from __future__ import annotations

from ecourts_client.models import StateRef

# District-court scope: 36 states + union territories, keyed by the numeric
# eCourts ``state_code`` (the value passed on to list_districts()).
_DISTRICT_STATES: tuple[StateRef, ...] = (
    StateRef(code="28", name="Andaman and Nicobar", national_code="AN"),
    StateRef(code="2", name="Andhra Pradesh", national_code="AP"),
    StateRef(code="36", name="Arunachal Pradesh", national_code="AR"),
    StateRef(code="6", name="Assam", national_code="AS"),
    StateRef(code="8", name="Bihar", national_code="BR"),
    StateRef(code="27", name="Chandigarh", national_code="CH"),
    StateRef(code="18", name="Chhattisgarh", national_code="CG"),
    StateRef(code="26", name="Delhi", national_code="DL"),
    StateRef(code="30", name="Goa", national_code="GA"),
    StateRef(code="17", name="Gujarat", national_code="GJ"),
    StateRef(code="14", name="Haryana", national_code="HR"),
    StateRef(code="5", name="Himachal Pradesh", national_code="HP"),
    StateRef(code="12", name="Jammu and Kashmir", national_code="JK"),
    StateRef(code="7", name="Jharkhand", national_code="JH"),
    StateRef(code="3", name="Karnataka", national_code="KA"),
    StateRef(code="4", name="Kerala", national_code="KL"),
    StateRef(code="33", name="Ladakh", national_code="LD"),
    StateRef(code="37", name="Lakshadweep", national_code="LW"),
    StateRef(code="23", name="Madhya Pradesh", national_code="MP"),
    StateRef(code="1", name="Maharashtra", national_code="MH"),
    StateRef(code="25", name="Manipur", national_code="MN"),
    StateRef(code="21", name="Meghalaya", national_code="MG"),
    StateRef(code="19", name="Mizoram", national_code="MZ"),
    StateRef(code="34", name="Nagaland", national_code="NL"),
    StateRef(code="11", name="Odisha", national_code="OD"),
    StateRef(code="35", name="Puducherry", national_code="PY"),
    StateRef(code="22", name="Punjab", national_code="PB"),
    StateRef(code="9", name="Rajasthan", national_code="RJ"),
    StateRef(code="24", name="Sikkim", national_code="SK"),
    StateRef(code="10", name="Tamil Nadu", national_code="TN"),
    StateRef(code="29", name="Telangana", national_code="TS"),
    StateRef(code="38", name="The Dadra And Nagar Haveli And Daman And Diu", national_code="DD"),
    StateRef(code="20", name="Tripura", national_code="TR"),
    StateRef(code="15", name="Uttarakhand", national_code="UK"),
    StateRef(code="13", name="Uttar Pradesh", national_code="UP"),
    StateRef(code="16", name="West Bengal", national_code="WB"),
)

# High-court scope: 25 High Courts, keyed by the numeric eCourts HC code. The
# ``national_code`` here is the HC-specific code the API returns (e.g. Madras HC
# = "HC", Telangana HC = "HB"), not always a geographic state code.
_HIGH_COURTS: tuple[StateRef, ...] = (
    StateRef(code="13", name="Allahabad High Court", national_code="UP"),
    StateRef(code="1", name="Bombay High Court", national_code="MH"),
    StateRef(code="16", name="Calcutta High Court", national_code="WB"),
    StateRef(code="6", name="Gauhati High Court", national_code="GA"),
    StateRef(code="29", name="High Court  for State of Telangana", national_code="HB"),
    StateRef(code="2", name="High Court of Andhra Pradesh", national_code="AP"),
    StateRef(code="18", name="High Court of Chhattisgarh", national_code="CG"),
    StateRef(code="26", name="High Court of Delhi", national_code="DL"),
    StateRef(code="17", name="High Court of Gujarat", national_code="GJ"),
    StateRef(code="5", name="High Court of Himachal Pradesh", national_code="HP"),
    StateRef(code="12", name="High Court of Jammu and Kashmir", national_code="JK"),
    StateRef(code="7", name="High Court of Jharkhand", national_code="JH"),
    StateRef(code="3", name="High Court of Karnataka", national_code="KA"),
    StateRef(code="4", name="High Court of Kerala", national_code="KL"),
    StateRef(code="23", name="High Court of Madhya Pradesh", national_code="MP"),
    StateRef(code="25", name="High Court of Manipur", national_code="MN"),
    StateRef(code="21", name="High Court of Meghalaya", national_code="ML"),
    StateRef(code="11", name="High Court of Orissa", national_code="OD"),
    StateRef(code="22", name="High Court of Punjab and Haryana", national_code="PH"),
    StateRef(code="9", name="High Court of Rajasthan", national_code="RJ"),
    StateRef(code="24", name="High Court of Sikkim", national_code="SK"),
    StateRef(code="20", name="High Court of Tripura", national_code="TR"),
    StateRef(code="15", name="High Court of Uttarakhand", national_code="UK"),
    StateRef(code="10", name="Madras High Court", national_code="HC"),
    StateRef(code="8", name="Patna High Court", national_code="BR"),
)

# Keyed by client ``.scope`` so the fallback wrapper can pick the right list
# without knowing which client it wraps.
STATIC_STATES: dict[str, tuple[StateRef, ...]] = {
    "district": _DISTRICT_STATES,
    "highcourt": _HIGH_COURTS,
}
