"""Per-High-Court WEBSITE cause-list VC-link sources, keyed by PRESIDING JUDGE.

The eCourts-served HC cause-list PDF carries NO VC links (verified 2026-07-06:
Delhi HC eCourts PDF has ``has_webex=False``). The Webex/VC join links are
published only on each High Court's OWN website, embedded per-court in the daily
cause-list PDFs.

Joining the two by court NUMBER fails: the eCourts cause-list rows carry no
court_no, and eCourts numbers benches differently from the website's physical
courtrooms. But BOTH sources name the PRESIDING JUDGE ("HON'BLE MR. JUSTICE
C.HARI SHANKAR"), so we key the website VC map by normalized presiding-judge
name and the indexer joins each eCourts bench's roster to it.

Sources are registered per CNR state-prefix (``"DL"`` -> Delhi HC). Each source
owns its site's URL discovery; the in-PDF ``COURT NO. / JUSTICE / <url>`` format
is common enough that the shared inline harvester supplies the court_no->url
extraction, and this module adds the court_no->judge mapping on top.

Opportunistic + fail-open: any network/parse error yields ``{}``.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import replace
from datetime import date
from typing import Protocol, runtime_checkable

import pdfplumber
import requests

from ecourts_client.vc.inline import harvest_vc_links_from_pdf
from ecourts_client.vc.models import VCAccess

log = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT = 40

_COURT_HDR = re.compile(r"COURT\s*NO\.?\s*[:.]?\s*(\d+[A-Z]?)", re.IGNORECASE)
_JUDGE_LINE = re.compile(r"HON'?BLE.*?JUSTICE", re.IGNORECASE)
# Honorifics/titles stripped when normalizing a judge name to a comparable key.
_TITLE = re.compile(r"HON'?BLE|JUSTICE|CHIEF|MRS?\.?|MS\.?|DR\.?|SMT\.?|THE", re.IGNORECASE)


def normalize_judge(text: str | None) -> str | None:
    """Reduce a judge/bench string to a match key on the PRESIDING judge.

    Takes the first judge (before the first comma -> the presiding judge on a
    multi-judge bench), strips honorifics/titles, drops every non-letter, and
    upper-cases. "HON'BLE MR. JUSTICE C.HARI SHANKAR, HON'BLE ... VINOD KUMAR"
    and "HON'BLE MR.JUSTICE C.HARI SHANKAR" both -> "CHARISHANKAR".
    """
    if not text:
        return None
    first = text.split(",")[0]
    first = _TITLE.sub(" ", first)
    key = re.sub(r"[^A-Za-z]", "", first).upper()
    return key or None


@runtime_checkable
class HCWebsiteVCSource(Protocol):
    """Fetch {normalized_presiding_judge -> VCAccess} for one date."""

    prefix: str

    def vc_by_judge_for_date(self, target: date) -> dict[str, VCAccess]:
        ...


class DelhiHCVCSource:
    """Delhi High Court (delhihighcourt.nic.in).

    Discovery: the cause-list index page is static HTML with direct
    ``/files/YYYY-MM/cause-list/<name>.pdf`` links. We take every linked PDF in
    the target month whose filename carries the target ``DD.MM``, and for each
    build ``{normalized_presiding_judge -> VCAccess}`` by (a) harvesting
    court_no->url via the shared inline harvester and (b) reading the
    ``COURT NO. NN`` / ``HON'BLE ... JUSTICE ...`` blocks to map court_no->judge.
    The court number is preserved on the VCAccess (``room``) for digest display.
    """

    prefix = "DL"
    _BASE = "https://delhihighcourt.nic.in"
    _INDEX = _BASE + "/web/cause-lists/cause-list"
    _PDF_RE = re.compile(r"/files/\d{4}-\d{2}/cause-list/[^\"'>\s]+\.pdf")

    def __init__(self, session: requests.Session | None = None) -> None:
        self._s = session or requests.Session()

    def _index_pdf_links(self) -> list[str]:
        r = self._s.get(self._INDEX, timeout=_TIMEOUT, headers=_UA)
        r.raise_for_status()
        return sorted(set(self._PDF_RE.findall(r.text)))

    @staticmethod
    def _matches_date(rel: str, target: date) -> bool:
        if f"/{target.year}-{target.month:02d}/" not in rel:
            return False
        return f"{target.day:02d}.{target.month:02d}" in rel

    @staticmethod
    def _court_to_judge(pdf_bytes: bytes) -> dict[str, str]:
        """Map court_no -> presiding-judge text by walking the PDF lines."""
        out: dict[str, str] = {}
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception:  # noqa: BLE001
            return {}
        cur: str | None = None
        for line in text.splitlines():
            hdr = _COURT_HDR.search(line)
            if hdr:
                cur = hdr.group(1)
                continue
            if cur is not None and _JUDGE_LINE.search(line):
                out.setdefault(cur, re.sub(r"\s+", " ", line).strip())
                cur = None  # first judge line under this court wins
        return out

    def vc_by_judge_for_date(self, target: date) -> dict[str, VCAccess]:
        out: dict[str, VCAccess] = {}
        try:
            links = self._index_pdf_links()
        except Exception as e:  # noqa: BLE001
            log.warning("DelhiHC VC index fetch failed: %s", e)
            return {}
        for rel in links:
            if not self._matches_date(rel, target):
                continue
            try:
                r = self._s.get(self._BASE + rel, timeout=_TIMEOUT, headers=_UA)
                if r.status_code != 200:
                    continue
                by_court = harvest_vc_links_from_pdf(r.content)     # court_no -> VCAccess
                court_judge = self._court_to_judge(r.content)        # court_no -> judge
            except Exception as e:  # noqa: BLE001
                log.warning("DelhiHC VC pdf %s failed: %s", rel, e)
                continue
            for court_no, access in by_court.items():
                jkey = normalize_judge(court_judge.get(court_no))
                if not jkey:
                    continue
                # keep the court number on the VCAccess for the digest display
                out.setdefault(jkey, replace(access, room=court_no))
        return out


_SOURCES: dict[str, HCWebsiteVCSource] = {}


def register(source: HCWebsiteVCSource) -> None:
    _SOURCES[source.prefix.upper()] = source


def get_hc_vc_source(prefix: str) -> HCWebsiteVCSource | None:
    """Return the website VC source for a CNR state prefix, or None."""
    return _SOURCES.get((prefix or "").upper())


register(DelhiHCVCSource())
