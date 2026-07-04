"""Harvest VC links printed inline in HC cause-list PDFs.

Post the SC e-Committee order (06-Oct-2023) many HCs print a per-court VC link
under each 'COURT NO. NN' header. This scans already-extracted PDF text, keys
links to the court header they sit under, de-wraps links split across a line
break, and de-spaces letter-spaced URLs. Opportunistic: returns whatever it
finds (Delhi HC verified), {} otherwise."""
from __future__ import annotations

import re
from ecourts_client.vc.models import VCAccess, VCLinkType, VCVendor

_COURT_HDR = re.compile(r"COURT\s*NO\.?\s*([0-9]+[A-Z]?)", re.IGNORECASE)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_MEETING = re.compile(r"MEETING\s*(?:NUMBER|NO\.?|ID)\s*[:.]?\s*([\d ]{6,})", re.IGNORECASE)
_PASSWORD = re.compile(r"PASSWORD\s*[:.]?\s*(\S+)", re.IGNORECASE)
_JOIN_HINT = re.compile(r"JOIN\s*V\.?\s*C\.?|MEETING\s*LINK", re.IGNORECASE)


def _despace(s: str) -> str:
    # Undo letter-spaced URLs ("h t t p s :/ / d i s t r i c t...") by removing
    # single spaces between non-space chars when they cluster inside a URL run.
    if s.count(" ") > len(s) / 3:
        return s.replace(" ", "")
    return s


def _classify(url: str) -> VCVendor:
    u = url.lower()
    if "webex.com" in u:
        return VCVendor.WEBEX_HOSTED if "j.php" in u else VCVendor.WEBEX_PERSONAL_ROOM
    if "teams.microsoft" in u:
        return VCVendor.MS_TEAMS
    if "meet.jit.si" in u:
        return VCVendor.JITSI
    if "meet.google" in u:
        return VCVendor.GOOGLE_MEET
    if "youtube.com" in u or "youtu.be" in u:
        return VCVendor.YOUTUBE_LIVESTREAM
    return VCVendor.CUSTOM


def harvest_vc_links(text: str) -> dict[str, VCAccess]:
    lines = text.splitlines()
    out: dict[str, VCAccess] = {}
    current: str | None = None
    i = 0
    while i < len(lines):
        raw = lines[i]
        hdr = _COURT_HDR.search(raw)
        if hdr:
            current = hdr.group(1)
            i += 1
            continue
        if current is not None and (_JOIN_HINT.search(raw) or _URL.search(raw)):
            # gather this line + the next (for wrapped URLs) and pull a URL
            blob = _despace(raw)
            m = _URL.search(blob)
            if m:
                url = m.group(0).rstrip(").,;")
                # de-wrap: a URL ending at line end with a bare token next line
                if i + 1 < len(lines) and (url.endswith("/") or url.endswith(".")):
                    nxt = _despace(lines[i + 1]).strip().split()[0] if lines[i + 1].strip() else ""
                    if nxt and not _URL.search(nxt) and " " not in nxt:
                        url = url + nxt
                        i += 1
                mid = _MEETING.search(text[text.find(raw):text.find(raw) + 300]) if raw in text else None
                pwd = _PASSWORD.search(text[text.find(raw):text.find(raw) + 300]) if raw in text else None
                vendor = _classify(url)
                out.setdefault(current, VCAccess(
                    vendor=vendor,
                    link_type=(VCLinkType.LIVESTREAM_URL
                               if vendor is VCVendor.YOUTUBE_LIVESTREAM else VCLinkType.JOIN_URL),
                    url=url,
                    meeting_id=(mid.group(1).strip() if mid else None),
                    passcode=(pwd.group(1).strip() if pwd else None),
                ))
        i += 1
    return out
