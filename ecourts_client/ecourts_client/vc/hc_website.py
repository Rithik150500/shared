"""Per-High-Court WEBSITE cause-list VC-link sources.

The eCourts-served HC cause-list PDF (``causelist_pdf.php``) carries NO VC links
-- verified 2026-07-06 against Delhi HC: ``has_webex=False``. The Webex/VC join
links are published only on each High Court's OWN website, embedded per-bench in
the daily cause-list PDFs under a ``COURT NO. NN`` header. This module fetches an
HC website's daily cause-list PDF(s) and runs the shared inline harvester
(:func:`ecourts_client.vc.inline.harvest_vc_links_from_pdf`) over them, returning
``{court_no: VCAccess}``.

Sources are registered per CNR state-prefix (``"DL"`` -> Delhi HC). Each source
owns its site's URL DISCOVERY (site layouts differ per HC) but reuses the shared
harvester, since the in-PDF ``COURT NO. / JOIN VC / <url>`` format is common
across post-2023 HC cause lists.

Opportunistic + fail-open: any network/parse error yields ``{}`` -- VC harvest
must never break indexing.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Protocol, runtime_checkable

import requests

from ecourts_client.vc.inline import harvest_vc_links_from_pdf
from ecourts_client.vc.models import VCAccess

log = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT = 40


@runtime_checkable
class HCWebsiteVCSource(Protocol):
    """Fetch {court_no -> VCAccess} from an HC website for one date."""

    prefix: str

    def vc_links_for_date(self, target: date) -> dict[str, VCAccess]:
        ...


class DelhiHCVCSource:
    """Delhi High Court (delhihighcourt.nic.in).

    Discovery: the cause-list index page is static HTML with direct
    ``/files/YYYY-MM/cause-list/<name>.pdf`` links (no JS/API). We take every
    linked PDF in the target month whose filename carries the target ``DD.MM``,
    fetch each, harvest, and merge (first court_no wins). Filenames are NOT
    standardised (``sup_2_06.07.pdf``, ``final_suppl.20.05.2025.pdf``,
    ``vk_sup_fnl_for_28.04.26.pdf``), so we match on the date, not the variant,
    and let the harvester return ``{}`` for any list that carries no VC links.
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
        # Month directory must match AND the filename must carry the day.month.
        if f"/{target.year}-{target.month:02d}/" not in rel:
            return False
        return f"{target.day:02d}.{target.month:02d}" in rel

    def vc_links_for_date(self, target: date) -> dict[str, VCAccess]:
        out: dict[str, VCAccess] = {}
        try:
            links = self._index_pdf_links()
        except Exception as e:  # noqa: BLE001 - never break indexing
            log.warning("DelhiHC VC index fetch failed: %s", e)
            return {}
        for rel in links:
            if not self._matches_date(rel, target):
                continue
            try:
                r = self._s.get(self._BASE + rel, timeout=_TIMEOUT, headers=_UA)
                if r.status_code != 200:
                    continue
                vc = harvest_vc_links_from_pdf(r.content)
            except Exception as e:  # noqa: BLE001
                log.warning("DelhiHC VC pdf %s failed: %s", rel, e)
                continue
            for court_no, access in vc.items():
                out.setdefault(court_no, access)  # first-seen wins across variants
        return out


_SOURCES: dict[str, HCWebsiteVCSource] = {}


def register(source: HCWebsiteVCSource) -> None:
    _SOURCES[source.prefix.upper()] = source


def get_hc_vc_source(prefix: str) -> HCWebsiteVCSource | None:
    """Return the website VC source for a CNR state prefix, or None."""
    return _SOURCES.get((prefix or "").upper())


register(DelhiHCVCSource())
