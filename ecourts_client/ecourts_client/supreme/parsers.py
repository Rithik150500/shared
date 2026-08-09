"""Parse the SC mobile case-detail HTML (pageid=030001) into a generic ``Case``.

The page is a server-rendered ``<table>`` of label→value rows (Diary No.,
Case No., Present/Last Listed On, Status/Stage, Petitioner(s)/Respondent(s),
advocates). Schema-tolerant: unknown/renamed labels are ignored, missing values
degrade to None rather than crashing.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup

from ecourts_client.errors import CNRNotFound
from ecourts_client.models import Case, Party
from ecourts_client.parsers.disposal import reads_as_disposed

_DATE_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")
_LEAD_NUM = re.compile(r"^\s*\d+\s+")  # "1 ABDUL RAIHAN MIAN" -> "ABDUL RAIHAN MIAN"


def _first_date(text: str | None) -> date | None:
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()
    except ValueError:
        return None


def _clean_party(text: str | None) -> str:
    if not text:
        return ""
    # take the first listed name; strip a leading serial number
    first = text.split("\n")[0].strip()
    return _LEAD_NUM.sub("", first).strip()


def _bench(listed: str | None) -> str | None:
    """Extract the bench (judges) from the '... [ HON'BLE ... ]' suffix."""
    if not listed:
        return None
    m = re.search(r"\[(.+?)\]?$", listed)
    if not m:
        return None
    b = m.group(1).strip().rstrip("]").strip()
    return b or None


def _clean_case_no(text: str | None) -> str:
    """'SLP(Crl) No. 003159 -  / 2026  Registered on ...' -> 'SLP(Crl) No. 003159/2026'."""
    if not text:
        return ""
    head = re.split(r"\bRegistered\b|\bVerified\b", text)[0]
    head = re.sub(r"\s*-\s*/\s*", "/", head)  # "003159 -  / 2026" -> "003159/2026"
    return re.sub(r"\s+", " ", head).strip()


# The SC page publishes the UPCOMING listing under its own label. The label is
# long and has drifted in wording, so match on the stable prefix rather than the
# whole string.
_TENTATIVE_PREFIX = "tentatively case may be listed on"

# Whether a listing is "past" is a question about an Indian court's day, and the
# droplet runs UTC — 5h30m behind IST, so a bare date.today() there is still on
# yesterday's date until 05:30 IST. Judge court dates on the court's own clock.
_IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> date:
    return datetime.now(_IST).date()


def _tentative_listing(d: dict[str, str]) -> date | None:
    """The 'Tentatively case may be listed on (likely to be listed on)' date."""
    for k, v in d.items():
        if k.strip().lower().startswith(_TENTATIVE_PREFIX):
            return _first_date(v)
    return None


def _next_listing(
    d: dict[str, str], *, stage: str | None, today: date | None = None
) -> date | None:
    """The case's NEXT listing date, or None when nothing is scheduled.

    ★★ 'Present/Last Listed On' is exactly what it says — the LAST listing. Using
    it as the next hearing made a dormant matter advertise a hearing that had
    already happened, sometimes years earlier: 72 of prod's 94 SC cases carried a
    past 'next hearing', one of them 2017-02-21 on a case refreshed daily. The
    UI renders any past next-hearing as a red "Overdue", so every one of those
    read as a missed hearing on a perfectly healthy case.

    The real upcoming date is published SEPARATELY, under 'Tentatively case may
    be listed on (likely to be listed on)' — a field this parser used to ignore
    completely. On the 25 stalest SC cases in prod, 7 carried it and all 7 were
    in the future; `50344/2024` was showing "Overdue since 2025-11-14" while
    actually listed four days out on 2026-08-14. Prefer it.

    Order of trust:
      1. the tentative/upcoming date, when it is not in the past;
      2. 'Present/Last Listed On' ONLY while it is still in the future (the SC
         does publish a forward listing there once a date is fixed);
      3. nothing — a dormant matter has no next hearing, and saying so plainly
         beats inventing one from history.
    """
    ref = today or _today_ist()
    tentative = _tentative_listing(d)
    if tentative is not None and tentative >= ref:
        candidate = tentative
    else:
        listed = _first_date(d.get("Present/Last Listed On"))
        candidate = listed if (listed is not None and listed >= ref) else None
    if candidate is None:
        return None
    # A disposed matter has no next hearing whatever the page still shows.
    if reads_as_disposed(stage=stage, next_hearing_date=candidate):
        return None
    return candidate


def _rows(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            k = tds[0].get_text(" ", strip=True).rstrip(":").strip()
            v = tds[1].get_text(" ", strip=True)
            if k and k not in out:
                out[k] = v
    return out


def parse_case_html(
    html: str, *, diary_no: str, diary_yr: str, today: date | None = None
) -> Case:
    """Parse the pageid=030001 case-detail HTML into a ``Case``.

    ``cnr`` carries the diary ``<no>/<yr>`` (the stable per-forum key + the value
    ``fetch_case`` round-trips on). Raises ``CNRNotFound`` when the page has no
    case rows (unknown diary / empty result). ``today`` is injectable so the
    next-listing rule (which is inherently relative to now) is testable."""
    d = _rows(html)
    # A valid case page has these labels; their absence = no such case.
    if not any(k in d for k in ("Diary No.", "Case No.", "Petitioner(s)", "Status/Stage")):
        raise CNRNotFound(cnr=f"{diary_no}/{diary_yr}")

    pet = _clean_party(d.get("Petitioner(s)"))
    res = _clean_party(d.get("Respondent(s)"))
    parties: list[Party] = []
    if pet:
        parties.append(Party(name=pet, role="petitioner", advocate=d.get("Pet. Advocate(s)") or None))
    if res:
        parties.append(Party(name=res, role="respondent", advocate=d.get("Resp. Advocate(s)") or None))

    case_no = _clean_case_no(d.get("Case No."))
    title = f"{pet} vs {res}" if pet and res else (pet or res or case_no or f"Diary {diary_no}/{diary_yr}")
    listed = d.get("Present/Last Listed On")
    stage = (d.get("Status/Stage") or "").strip() or None
    next_hearing = _next_listing(d, stage=stage, today=today)

    return Case(
        cnr=f"{diary_no}/{diary_yr}",
        title=title,
        court="Supreme Court of India",
        stage=stage,
        next_hearing_date=next_hearing,
        judge=_bench(listed),
        parties=parties,
        history=[],
        orders=[],
        filing_date=_first_date(d.get("Diary No.")),  # "... Filed on DD-MM-YYYY ..."
    )
