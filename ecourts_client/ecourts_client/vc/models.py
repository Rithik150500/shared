"""Typed courtroom video-conference access record + room key.

VC publication is decentralised and vendor-heterogeneous (Webex personal-room
vs hosted, MS Teams, Jitsi, Google Meet, portal-login, YouTube livestream), so
a flat URL string is insufficient. Providers return this record or None."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class VCVendor(str, Enum):
    WEBEX_PERSONAL_ROOM = "webex_personal_room"
    WEBEX_HOSTED = "webex_hosted"
    MS_TEAMS = "ms_teams"
    JITSI = "jitsi"
    GOOGLE_MEET = "google_meet"
    JIOMEET = "jiomeet"
    VCONSOL_PORTAL = "vconsol_portal"
    YOUTUBE_LIVESTREAM = "youtube_livestream"
    CUSTOM = "custom"


class VCLinkType(str, Enum):
    JOIN_URL = "join_url"            # directly joinable
    LIVESTREAM_URL = "livestream_url"  # watch-only
    PORTAL_LOGIN = "portal_login"   # needs OTP/creds, no static URL


@dataclass(frozen=True)
class VCAccess:
    vendor: VCVendor
    link_type: VCLinkType
    url: str | None = None          # None for portal_login vendors
    meeting_id: str | None = None
    passcode: str | None = None
    requires_intimation: bool = False
    persistent: bool = True
    # Physical courtroom / room number as published in the VC directory
    # (e.g. "611", "137A"). Distinct from the eCourts court_no; used for
    # human display in the digest where the directory provides it.
    room: str | None = None

    def to_meta(self) -> dict[str, str | bool | None]:
        """Compact dict for the cause_list_rows.vc_meta JSON column."""
        return {
            "vendor": self.vendor.value, "link_type": self.link_type.value,
            "meeting_id": self.meeting_id, "passcode": self.passcode,
            "requires_intimation": self.requires_intimation, "persistent": self.persistent,
            "room": self.room,
        }


# (scope, court_complex_code, court_no) — all lowercased/stripped.
VCRoomKey = tuple[str, str, str]


def make_key(scope: str, court_complex_code: str, court_no: str) -> VCRoomKey:
    return (scope.strip().lower(), court_complex_code.strip().lower(), str(court_no).strip().lower())


# Regex to normalise spacing around a trailing "-NN" suffix (e.g. "- 01", " - 01").
_SUFFIX_RE = re.compile(r"\s*-\s*(\d+)$")
# Bracketing/separator punctuation the eCourts and directory vocabularies disagree
# on (e.g. "(Commercial Court)" vs ", Commercial Court"). Note: the "-" of a "-NN"
# suffix is deliberately NOT here so _SUFFIX_RE can still normalise it.
_PUNCT_RE = re.compile(r"[(),.'\"/]")


def normalize_designation(s: str | None) -> str:
    """Normalise a judicial-officer designation for use as a VC-map lookup key.

    Rules (in order): lowercase; ``&`` → ``and``; drop bracketing punctuation
    ``( ) , . ' " /``; collapse whitespace; normalise the trailing ``-NN`` suffix
    so "District Judge- 01" / "District Judge - 01" → "district judge-01".

    The ``&``/``and`` and punctuation steps bridge real formatting differences
    between the court directories and eCourts ``desgname`` — verified live: they
    roughly doubled the district match rate (5 → 11 of 27 sampled cases;
    e.g. "Principal District **&** Sessions Judge" vs "…**and**…", and
    "District Judge **(Commercial Court)**-01" vs "District Judge**,** Commercial
    Court-01"). Abbreviation/vocabulary mismatches (e.g. Rohtak "CJ(JD) cum JMIC"
    vs "Civil Judge (Junior Division)") are NOT bridged and remain unmatched.
    """
    if not s:
        return ""
    s = s.lower()
    s = s.replace("&", "and")
    s = _PUNCT_RE.sub(" ", s)
    # Collapse internal whitespace.
    s = " ".join(s.split())
    # Normalise trailing "-NN" spacing (including spaces before/after the dash).
    s = _SUFFIX_RE.sub(lambda m: f"-{m.group(1)}", s)
    return s
