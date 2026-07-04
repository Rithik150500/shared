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

    def to_meta(self) -> dict[str, str | bool | None]:
        """Compact dict for the cause_list_rows.vc_meta JSON column."""
        return {
            "vendor": self.vendor.value, "link_type": self.link_type.value,
            "meeting_id": self.meeting_id, "passcode": self.passcode,
            "requires_intimation": self.requires_intimation, "persistent": self.persistent,
        }


# (scope, court_complex_code, court_no) — all lowercased/stripped.
VCRoomKey = tuple[str, str, str]


def make_key(scope: str, court_complex_code: str, court_no: str) -> VCRoomKey:
    return (scope.strip().lower(), court_complex_code.strip().lower(), str(court_no).strip().lower())


# Regex to normalise spacing around a trailing "-NN" suffix (e.g. "- 01", " - 01").
_SUFFIX_RE = re.compile(r"\s*-\s*(\d+)$")


def normalize_designation(s: str | None) -> str:
    """Normalise a judicial-officer designation for use as a VC-map lookup key.

    Rules (in order):
    - None or empty → "".
    - Lowercase.
    - Collapse internal whitespace to single spaces, strip leading/trailing.
    - Normalise the ``-NN`` suffix so "District Judge- 01", "District Judge-01",
      and "District Judge - 01" all become "district judge-01".

    The directory text already matches eCourts vocabulary; only whitespace/case
    and the suffix-space differ, so NO abbreviation dictionary is needed.
    """
    if not s:
        return ""
    s = s.lower()
    # Collapse internal whitespace.
    s = " ".join(s.split())
    # Normalise trailing "-NN" spacing (including spaces before/after the dash).
    s = _SUFFIX_RE.sub(lambda m: f"-{m.group(1)}", s)
    return s
