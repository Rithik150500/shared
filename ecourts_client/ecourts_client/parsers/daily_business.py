"""Parser for `s_show_business.php` -- the 'View Business' panel for one hearing date.

The response is `{"viewBusiness": "<inline HTML>"}`. The HTML carries a header table
with case metadata, a free-text "Business Details" cell, and a footer noting the next
purpose / next hearing date.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from bs4 import BeautifulSoup

from ecourts_client.errors import SchemaChanged
from ecourts_client.models import DailyBusiness


def parse_daily_business(response: dict[str, Any], cnr: str, business_date: date) -> DailyBusiness:
    if not isinstance(response, dict):
        raise SchemaChanged(field="response", reason=f"expected dict, got {type(response).__name__}")
    html = response.get("viewBusiness")
    if not isinstance(html, str) or not html.strip():
        raise SchemaChanged(field="response.viewBusiness", reason="missing or empty viewBusiness HTML")

    soup = BeautifulSoup(html, "lxml")

    # Business text -- find the table cell that follows a "Business Details" / "Details of Business" label,
    # falling back to the longest text cell in the document.
    business_text = _extract_business_text(soup)

    # Next purpose + next hearing date are typically labelled in trailing rows.
    next_purpose = _extract_labelled(soup, ("next purpose", "purpose of next hearing"))
    next_date_str = _extract_labelled(soup, ("next hearing date", "next date", "date of next hearing"))
    next_hearing_date = _parse_dmy(next_date_str)

    return DailyBusiness(
        cnr=cnr,
        business_date=business_date,
        business_text=business_text,
        next_purpose=next_purpose,
        next_hearing_date=next_hearing_date,
    )


def _extract_business_text(soup: BeautifulSoup) -> str:
    # Strategy 1: cell whose neighbour says "Business" / "Details"
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        for i, c in enumerate(cells):
            label = c.get_text(strip=True).lower()
            if "business" in label and "details" in label and i + 1 < len(cells):
                txt = cells[i + 1].get_text("\n", strip=True)
                if txt:
                    return txt
    # Strategy 2: longest text cell (the panel often has just one substantial cell)
    candidates = [
        td.get_text("\n", strip=True)
        for td in soup.find_all(["td", "div", "p"])
    ]
    candidates.sort(key=len, reverse=True)
    return candidates[0] if candidates else ""


def _extract_labelled(soup: BeautifulSoup, labels: tuple[str, ...]) -> str | None:
    """Find a value cell whose preceding label cell loosely matches one of `labels`."""
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        for i, c in enumerate(cells):
            label = c.get_text(strip=True).lower()
            if any(l in label for l in labels) and i + 1 < len(cells):
                value = cells[i + 1].get_text(strip=True)
                if value:
                    return value
    return None


def _parse_dmy(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    if not s or s.lower() in ("date not given", "null", "none"):
        return None
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None
