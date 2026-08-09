"""TDSAT (Telecom Disputes Settlement & Appellate Tribunal) tribunal-kind adapter.

Verified transport (``docs/spike-tribunal-transport.md``): single New Delhi bench,
plain PHP, no captcha, ONE hop —
``POST tdsat.gov.in/Delhi/services/checkhomedetail1.php``
(``pet_type=1&casetype=<code>&caseno=<n>&caseyear=<yyyy>&submit1=Search``) returns
a rich CASE STATUS page (label→value rows + party blocks + a proceeding table).

Identifier: ``"<casetype>:<caseno>:<caseyear>"`` (e.g. ``"2:1:2023"``; casetype is
the numeric TDSAT code — 2=Telecom Petition, 4=Telecom Appeal, …).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import ClassVar

import requests

from ecourts_client._order_text import BLOCK_SEPARATOR, clean_order_text
from ecourts_client.errors import CNRNotFound, CourtSiteDown, ECourtsError, RateLimited
from ecourts_client.forums import Forum, ForumCapabilities, IdentifierKind, TribunalKind
from ecourts_client.models import Case, HearingHistoryRow, OrderRef, Party
from ecourts_client.tribunal._html import (
    extract_after_dash,
    label_value_map,
    parse_dmy,
    proceeding_table_rows,
)
from bs4 import BeautifulSoup

BASE_URL = "https://tdsat.gov.in/Delhi/services"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_TIMEOUT = 40
# Order-sheet links in the proceeding table: `popsurety_pet_adv_name('<b64>')`,
# where <b64> is the daily_order_view/orderp `filing_no` param for that order.
_ORDER_LINK_RE = re.compile(r"popsurety_pet_adv_name\(['\"]([^'\"]+)['\"]\)")
_DMY_RE = re.compile(r"\d{2}[-/]\d{2}[-/]\d{4}")

# How far past an order link to look for its own Order Date cell. The real
# ORDER DETAILS row puts ~350 chars of party detail between the two; the bound
# stops a dateless row from reaching down the page for an unrelated date.
_ORDER_DATE_LOOKAHEAD = 1200


def _max_inline_default() -> int:
    """Newest N order texts to fetch per case (env-tunable; 0 = all)."""
    try:
        return max(0, int(os.environ.get("TRIBUNAL_MAX_INLINE_ORDERS", "3")))
    except ValueError:
        return 3


def _extract_order_links(html: str) -> list[tuple[str | None, str]]:
    """Distinct (order_date_str, b64_id) order sheets, de-duped by base64 id.

    ★ The date belongs to the ORDER DETAILS row and FOLLOWS its link:

        | Serial No. | Case No. (link) | Party Detail | Order Date |

    That table sits BELOW the whole hearing table, so the old rule — nearest
    PRECEDING date — resolved every link to the same value: the case's latest
    hearing date. Compounding it, the Order Date column renders DD-MM-YYYY while
    the pattern only matched DD/MM/YYYY, so the true dates were invisible even
    where the direction was right. Live on 2026-08-09 all 13 orders of
    Broadcasting Petition/345/2024 came back stamped 2026-05-25.

    So: take the first date AFTER the link, bounded by the next order link (a row
    can never borrow its neighbour's date) and by ``_ORDER_DATE_LOOKAHEAD``. Fall
    back to the nearest preceding date for the older shape, where the link sits
    inside a hearing row and the date precedes it.
    """
    out: list[tuple[str | None, str]] = []
    seen: set[str] = set()
    matches = list(_ORDER_LINK_RE.finditer(html))
    for i, m in enumerate(matches):
        b64 = m.group(1)
        if b64 in seen:
            continue
        seen.add(b64)
        stop = min(
            matches[i + 1].start() if i + 1 < len(matches) else len(html),
            m.end() + _ORDER_DATE_LOOKAHEAD,
        )
        after = _DMY_RE.search(html, m.end(), stop)
        if after:
            out.append((after.group(0), b64))
            continue
        before = _DMY_RE.findall(html[: m.start()])
        out.append((before[-1] if before else None, b64))
    return out


def _split_identifier(identifier: str) -> tuple[str, str, str]:
    parts = (identifier or "").split(":")
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise ECourtsError(f"TDSAT identifier must be '<casetype>:<caseno>:<caseyear>', got {identifier!r}")
    return tuple(p.strip() for p in parts)  # type: ignore[return-value]


def _adv(text: str, label: str) -> str | None:
    m = re.search(rf"{re.escape(label)}\s*:?-?\s*(.+?)\s*(?:Additional|Respondent|Petitioner|$)", text, re.I | re.S)
    if not m:
        return None
    v = re.sub(r"\s+", " ", m.group(1)).strip(" -,:")
    return v or None


def parse_status_html(html: str, *, casetype: str, caseno: str, caseyear: str) -> Case:
    """Parse the TDSAT CASE STATUS page into a ``Case``. Raises ``CNRNotFound``
    when the page carries no case (unknown {casetype,caseno,caseyear})."""
    soup = BeautifulSoup(html, "html.parser")
    d = label_value_map(soup)
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    ref = d.get("Case Type/Case No/Year")
    if not ref and "Case Status" not in d:
        raise CNRNotFound(cnr=f"tdsat:{casetype}:{caseno}:{caseyear}")

    pet = extract_after_dash(text, "Petitioner Name")
    res = extract_after_dash(text, "Respondent Name")
    parties: list[Party] = []
    if pet:
        parties.append(Party(name=pet, role="petitioner", advocate=_adv(text, "Pet. Advocate Name")))
    if res:
        parties.append(Party(name=res, role="respondent", advocate=_adv(text, "Respondent Advocate")))

    status = d.get("Case Status") or ""
    disposal = d.get("Disposal Nature")
    stage = status + (f" — {disposal}" if disposal and status.lower().startswith("dispos") else "")

    history: list[HearingHistoryRow] = []
    for row in proceeding_table_rows(soup, "Hearing Date", 5):
        # [Bench No, Hearing Date, Purpose, Status, Order]
        hd = parse_dmy(row[1])
        if hd:
            history.append(HearingHistoryRow(hearing_date=hd, purpose=row[2], judge=""))

    # Order metadata only (pure). TDSAT has no order PDFs — the order text (HTML)
    # is fetched by the client's _inline_order_text for the newest few.
    orders: list[OrderRef] = []
    for dstr, b64 in _extract_order_links(html):
        od = parse_dmy(dstr)
        if od:
            orders.append(
                OrderRef(
                    order_date=od,
                    order_url=f"{BASE_URL}/daily_order_view.php?filing_no={b64}",
                    order_id=b64,
                )
            )

    title = f"{pet} vs {res}" if pet and res else (pet or res or ref or f"{casetype}/{caseno}/{caseyear}")
    return Case(
        cnr=(ref or f"{casetype}/{caseno}/{caseyear}").replace(" ", " ").strip(),
        title=title,
        court="Telecom Disputes Settlement and Appellate Tribunal, New Delhi",
        stage=stage or None,
        next_hearing_date=None,  # TDSAT status page exposes no clean 'next date'
        judge=None,
        parties=parties,
        history=history,
        orders=orders,
        filing_date=parse_dmy(d.get("Date of Filing")),
    )


@dataclass
class TDSATClient:
    """``ForumAdapter`` for the TDSAT tribunal kind (Forum.TRIBUNAL / kind=TDSAT)."""

    scope: str = "tribunal_tdsat"
    base_url: str = BASE_URL
    max_inline_orders: int = field(default_factory=_max_inline_default)
    capabilities: ClassVar[ForumCapabilities] = ForumCapabilities(
        forum=Forum.TRIBUNAL,
        identifier_kind=IdentifierKind.TRIBUNAL_CASE_NO,
        supports_fetch=True,
        supports_search=False,
        supports_pdf=False,
        is_manual=False,
        tribunal_kind=TribunalKind.TDSAT,
    )
    _http: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": _UA})

    def fetch_case(self, identifier: str) -> Case:
        casetype, caseno, caseyear = _split_identifier(identifier)
        try:
            resp = self._http.post(
                f"{self.base_url}/checkhomedetail1.php",
                data={"pet_type": "1", "casetype": casetype, "caseno": caseno, "caseyear": caseyear, "submit1": "Search"},
                timeout=_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            raise CourtSiteDown(f"TDSAT connection error: {e}") from e
        if resp.status_code == 429:
            raise RateLimited("TDSAT returned 429")
        if resp.status_code >= 500:
            raise CourtSiteDown(f"TDSAT {resp.status_code}")
        case = parse_status_html(resp.text or "", casetype=casetype, caseno=caseno, caseyear=caseyear)
        return self._inline_order_text(case)

    def _fetch_order_text(self, b64: str) -> str | None:
        """GET orderp.php?filing_no=<b64> → the order-sheet text (TDSAT serves
        orders as HTML, not PDF). Best-effort; capped. None on any error.

        ★ orderp.php raises in dompdf AFTER emitting the order, so the response
        carries a PHP ``Fatal error`` banner + stack trace at the end of EVERY
        order sheet — HTTP 200 the whole way, so there is nothing to detect at
        the transport layer. ``clean_order_text`` strips it; without that the
        banner rides through to whatever renders the order for the user."""
        try:
            resp = self._http.get(
                f"{self.base_url}/orderp.php", params={"filing_no": b64}, timeout=_TIMEOUT
            )
        except (requests.ConnectionError, requests.Timeout):
            return None
        if resp.status_code != 200:
            return None
        return clean_order_text(
            BeautifulSoup(resp.text or "", "html.parser").get_text(
                BLOCK_SEPARATOR, strip=True
            )
        )

    def _inline_order_text(self, case: Case) -> Case:
        """Fetch order text for the newest ``max_inline_orders`` orders (0 => all),
        set it on ``OrderRef.order_text`` → the timeline shows the order content.
        Uncapped orders keep metadata only. Best-effort."""
        if not case.orders:
            return case
        by_recency = sorted(
            range(len(case.orders)),
            key=lambda i: case.orders[i].order_date or date.min,
            reverse=True,
        )
        take = set(by_recency if self.max_inline_orders == 0 else by_recency[: self.max_inline_orders])
        new_orders = list(case.orders)
        for i in take:
            t = self._fetch_order_text(new_orders[i].order_id)
            if t:
                new_orders[i] = replace(new_orders[i], order_text=t)
        return replace(case, orders=new_orders)

    def fetch_pdf(self, url: str) -> bytes:
        raise NotImplementedError("TDSAT orders are HTML; text is inlined at fetch time (order_text)")
