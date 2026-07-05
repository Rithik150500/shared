"""Shared HTML helpers for the NIC-portal tribunal adapters (DRT, TDSAT, …).

These portals render a case as a mix of 2-cell ``<tr>`` label→value rows
(Diary/Case Type/Status/Next Listing …), ``-``-prefixed party-name blocks, and a
proceeding ``<table>``. All dates are ``DD/MM/YYYY``. Kept schema-tolerant:
missing/renamed labels degrade to None rather than crashing.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup, Tag

_DMY = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def parse_dmy(text: str | None) -> date | None:
    """First ``DD/MM/YYYY`` in ``text`` → date; None if absent/invalid."""
    if not text:
        return None
    m = _DMY.search(text)
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()
    except ValueError:
        return None


def label_value_map(soup: BeautifulSoup) -> dict[str, str]:
    """Map every 2-cell ``<tr>`` (label → value); first occurrence of a label wins.

    Labels are normalised (trailing ``.``/``:`` and whitespace stripped)."""
    out: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) == 2:
            k = re.sub(r"\s+", " ", cells[0].get_text(" ", strip=True)).rstrip(".:").strip()
            v = re.sub(r"\s+", " ", cells[1].get_text(" ", strip=True)).strip()
            if k and k not in out:
                out[k] = v
    return out


def extract_after_dash(text: str, label: str) -> str | None:
    """Pull the value after ``<label>   -<value>`` (the portals' party-name idiom),
    stopping at the next 'Additional'/'Advocate'/'Address' section word."""
    m = re.search(
        rf"{re.escape(label)}\s*-\s*(.+?)\s*(?:Additional|Advocate|Address|Respondent|Petitioner|$)",
        text, re.I | re.S,
    )
    if not m:
        return None
    v = re.sub(r"\s+", " ", m.group(1)).strip(" -,")
    return v or None


def proceeding_table_rows(soup: BeautifulSoup, header_kw: str, ncols: int) -> list[list[str]]:
    """Return the data rows of the proceeding ``<table>`` whose header contains
    ``header_kw`` (e.g. 'Hearing Date' / 'Causelist Date'), as ``ncols``-cell lists.

    Targets the specific table (not a global ``tr`` sweep) to avoid the nested-
    table over-capture these malformed pages produce; de-dupes repeated rows.
    """
    header = soup.find(string=re.compile(re.escape(header_kw), re.I))
    if header is None:
        return []
    table = header.find_parent("table")
    if not isinstance(table, Tag):
        return []
    seen: set[tuple[str, ...]] = set()
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        # a genuine data row has exactly ncols cells and no header word
        if len(cells) != ncols or header_kw.lower() in " ".join(cells).lower():
            continue
        key = tuple(cells)
        if key in seen:
            continue
        seen.add(key)
        rows.append(cells)
    return rows
