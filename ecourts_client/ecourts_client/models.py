from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Literal


PartyRole = Literal["petitioner", "respondent", "complainant", "accused", "advocate", "other"]


@dataclass(frozen=True)
class Party:
    name: str
    role: PartyRole
    advocate: str | None = None


@dataclass(frozen=True)
class Act:
    act_name: str
    section: str | None = None


@dataclass(frozen=True)
class FIRDetails:
    fir_number: str
    police_station: str
    year: int


@dataclass(frozen=True)
class HearingHistoryRow:
    hearing_date: date
    purpose: str
    judge: str
    business_on_date: str | None = None


@dataclass(frozen=True)
class OrderRef:
    order_date: date
    order_url: str
    order_id: str
    # Some forums (e-Jagriti/Consumer) return the order PDF INLINE as base64 on
    # the case row rather than as a fetchable URL. When set, callers should
    # decode + store it directly (no second fetch) and MUST strip it before
    # persisting the Case JSON so the case blob doesn't balloon. None for
    # URL-based forums (eCourts).
    inline_pdf_b64: str | None = None


@dataclass(frozen=True)
class ObjectionDetails:
    raised_on: date
    objections: list[str]
    comply_by: date | None


@dataclass(frozen=True)
class CategoryDetails:
    category: str
    sub_category: str | None


@dataclass(frozen=True)
class StateRef:
    """A state in eCourts' geographic hierarchy.

    `national_code` is the 2-letter ISO-style prefix used in CNRs (e.g. 'DL').
    `code` is the numeric state code used by the API (e.g. '7' for Delhi).
    """
    code: str
    name: str
    national_code: str


@dataclass(frozen=True)
class DistrictRef:
    code: str
    name: str
    state_code: str


@dataclass(frozen=True)
class CourtComplexRef:
    """A court complex within a district.

    The mobile API exposes two parallel codes per complex:
    - ``code`` is ``complex_code`` (e.g. ``"1080060-2"`` for states that
      inline the establishment, or a bare ``"1140041"`` for states that
      don't) — an identifier for the complex row itself.
    - ``est_code`` is ``njdg_est_code`` (e.g. ``"2"`` or a CSV ``"1,3,4"``
      for a multi-establishment complex) — this is the value the app stores
      as ``SESSION_COURT_CODE`` and the ONLY one the data endpoints accept:
      SEARCH endpoints take the full est CSV in ``court_code_arr``; LISTING
      endpoints (caseNumberWebService.php, policeStationWebService.php) take
      a single est in ``court_code``. Passing ``code`` to either returns
      ``{status:"N", msg:"error"}`` / 0 establishments with no field detail.
    """
    code: str
    name: str
    state_code: str
    district_code: str
    est_code: str = ""


@dataclass(frozen=True)
class PoliceStationRef:
    """A police station within a court complex's jurisdiction.

    `uniform_code` is a separate national identifier needed by the FIR-search endpoint;
    `0` means not assigned.
    """
    code: str
    name: str
    district_code: str
    court_code: str
    uniform_code: int = 0


@dataclass(frozen=True)
class CaseTypeRef:
    """A case type code within one establishment (e.g. 'HMA - HINDU MARRIAGE ACT')."""
    code: str
    name: str
    court_code: str


@dataclass(frozen=True)
class BenchRef:
    """A bench of a high court (e.g. Madurai bench of Madras HC)."""
    code: str
    name: str
    state_code: str


@dataclass(frozen=True)
class CaseStub:
    """A search-result row. Just enough to follow up with fetch_case(cnr)."""
    cnr: str
    title: str
    case_number: str
    court: str
    filing_year: int | None = None
    stage: str | None = None


@dataclass(frozen=True)
class HCCauseListPDFRow:
    """A single case row extracted from an HC cause-list PDF.

    Position-based extraction is heuristic: HC PDFs vary in layout per bench, so
    raw_text is the canonical record (the multi-line text bundle for the case)
    and the structured columns (case_number, parties, advocates) are best-effort
    splits. Use raw_text when you need fidelity, columns when you need filterable.
    """
    sr_no: int
    section: str  # e.g. "LUNCH MOTION / ADMISSION"
    case_number: str  # best-effort: the leftmost token after sr_no
    raw_text: str  # full multi-line text of the row, in reading order
    parties: str = ""
    advocates: str = ""
    court_no: str | None = None  # "COURT NO. NN" header this row sits under; None when the PDF has no such header


@dataclass(frozen=True)
class HCCauseListIndex:
    """One row of the HC cause-list index (cases_new.php for HC scope).

    Unlike district, the HC API returns an index of (bench, list_type, PDF link) --
    each entry points to a downloadable PDF whose actual case rows live inside.
    Parse the PDF separately via pdfplumber to extract individual case rows.
    """
    sr_no: int
    bench: str
    list_type: str  # e.g. "FRESH MOTION HEARING", "LUNCH MOTION", "REGULAR MOTION"
    pdf_url: str


@dataclass(frozen=True)
class HCBenchSitting:
    """A bench sitting on a particular date, returned by causeListBenchWebService.php."""
    code: str
    name: str
    state_code: str
    sitting_date: date


@dataclass(frozen=True)
class CauseListEntry:
    """One row of a cause list.

    The cause-list endpoint provides `case_number` (the uniform 16-digit case-id) but
    NOT the CNR -- to resolve the CNR you need a separate listOfCasesWebService.php
    call. We expose both fields so callers can do that follow-up if needed.
    """
    sr_no: int
    case_number: str
    cnr: str | None  # None unless the row carries a CNR explicitly (rare)
    parties: str
    advocates: str | None
    section: str  # "Misc. cases" / "Final Disposal Misc." / etc.
    listed_on: date | None


@dataclass(frozen=True)
class CauseList:
    """A district cause list for one (court_code, court_no) on one date."""
    state_code: str
    district_code: str
    court_code: str
    court_no: str
    list_date: date
    flag: str  # "civ_t" or "cri_t"
    judge: str | None
    entries: list[CauseListEntry] = field(default_factory=list)


@dataclass(frozen=True)
class DailyBusiness:
    """The 'View Business' panel for a particular hearing date."""
    cnr: str
    business_date: date
    business_text: str
    next_purpose: str | None
    next_hearing_date: date | None


@dataclass(frozen=True)
class Case:
    cnr: str
    title: str
    court: str
    stage: str
    next_hearing_date: date | None
    judge: str | None
    parties: list[Party] = field(default_factory=list)
    acts: list[Act] = field(default_factory=list)
    history: list[HearingHistoryRow] = field(default_factory=list)
    orders: list[OrderRef] = field(default_factory=list)
    fir: FIRDetails | None = None
    objections: ObjectionDetails | None = None
    category: CategoryDetails | None = None
    # Filing date from the case-detail API (`date_of_filing`). Optional/last so
    # existing positional callers keep working. Powers the "Filed" timeline event.
    filing_date: date | None = None
    # Routing facts to LOCATE this case's cause list (district). court_no +
    # case_no are top-level in the caseHistory response (verified live:
    # court_no='75'); numeric state/district/court_code come from order rows.
    # Optional/last so positional callers keep working; serialised into
    # snapshots via to_json/from_json so cached Cases retain routing facts.
    case_no: str | None = None
    court_no: str | None = None
    state_code: str | None = None
    district_code: str | None = None
    court_code: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=_json_default, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Case:
        d = json.loads(raw)
        return _case_from_dict(d)


def _json_default(o: object) -> str:
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(f"Cannot JSON-encode {type(o).__name__}")


def _case_from_dict(d: dict) -> Case:
    def _date(s: str | None) -> date | None:
        return date.fromisoformat(s) if s else None

    parties = [Party(**p) for p in d["parties"]]
    acts = [Act(**a) for a in d["acts"]]
    history = [HearingHistoryRow(**{**h, "hearing_date": _date(h["hearing_date"])}) for h in d["history"]]
    orders = [OrderRef(**{**o, "order_date": _date(o["order_date"])}) for o in d["orders"]]
    fir = FIRDetails(**d["fir"]) if d.get("fir") else None
    objections = (
        ObjectionDetails(
            raised_on=_date(d["objections"]["raised_on"]),
            objections=d["objections"]["objections"],
            comply_by=_date(d["objections"]["comply_by"]),
        )
        if d.get("objections")
        else None
    )
    category = CategoryDetails(**d["category"]) if d.get("category") else None
    return Case(
        cnr=d["cnr"],
        title=d["title"],
        court=d["court"],
        stage=d["stage"],
        next_hearing_date=_date(d["next_hearing_date"]),
        judge=d["judge"],
        parties=parties,
        acts=acts,
        history=history,
        orders=orders,
        fir=fir,
        objections=objections,
        category=category,
        filing_date=_date(d.get("filing_date")),
        case_no=d.get("case_no"),
        court_no=d.get("court_no"),
        state_code=d.get("state_code"),
        district_code=d.get("district_code"),
        court_code=d.get("court_code"),
    )
