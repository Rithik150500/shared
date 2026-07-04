"""Supreme Court forum adapter (com.nic.sciapp mobile backend).

Confirmed transport (see ``docs/RE_NOTES_sci.md``): a token-gated backend at
``scourtapp.sci.gov.in``, routed by ``?pageid=<code>&token=<T>``. Case-status is
``pageid=030001`` + ``d_no``/``d_yr`` (diary number + year) → HTML case detail.

Identifier (``ForumAdapter.fetch_case``) is the composite ``"<diaryNo>:<diaryYr>"``.
The token comes from ``SC_MOBILE_TOKEN`` (a device-captured session token; see
``_session.py``). Party-name / case-number search + order-PDF fetch are follow-ups
(``supports_search``/``supports_pdf`` = False for now).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from ecourts_client.errors import IdentifierMalformed
from ecourts_client.forums import Forum, ForumCapabilities, IdentifierKind
from ecourts_client.models import Case
from ecourts_client.supreme._session import SupremeSession
from ecourts_client.supreme.parsers import parse_case_html

_PAGEID_CASE_STATUS = "030001"


def _split_identifier(identifier: str) -> tuple[str, str]:
    """Parse the composite ``"<diaryNo>:<diaryYr>"`` fetch identifier."""
    if not identifier or ":" not in identifier:
        raise IdentifierMalformed(
            forum=Forum.SUPREME_COURT.value,
            identifier=identifier,
            reason="expected '<diaryNo>:<diaryYr>'",
        )
    dno, _, dyr = identifier.partition(":")
    dno, dyr = dno.strip(), dyr.strip()
    if not dno or not dyr.isdigit():
        raise IdentifierMalformed(
            forum=Forum.SUPREME_COURT.value,
            identifier=identifier,
            reason="diaryNo non-empty and diaryYr numeric",
        )
    return dno, dyr


@dataclass
class SupremeCourtClient:
    """``ForumAdapter`` for the Supreme Court forum."""

    scope: str = "supreme_court"
    capabilities: ClassVar[ForumCapabilities] = ForumCapabilities(
        forum=Forum.SUPREME_COURT,
        identifier_kind=IdentifierKind.DIARY_NUMBER,
        supports_fetch=True,
        supports_search=False,   # party/case-no search = follow-up
        supports_pdf=False,      # order-PDF fetch = follow-up (separate pageid)
        is_manual=False,
    )
    _session: SupremeSession = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._session = SupremeSession()

    def fetch_case(self, identifier: str) -> Case:
        """Fetch a case by the composite ``"<diaryNo>:<diaryYr>"``.

        Raises CNRNotFound (no such diary), SCTokenMissing/SCTokenInvalid
        (token not set / expired), or CourtSiteDown/RateLimited on transport."""
        diary_no, diary_yr = _split_identifier(identifier)
        html = self._session.get(_PAGEID_CASE_STATUS, {"d_no": diary_no, "d_yr": diary_yr})
        return parse_case_html(html, diary_no=diary_no, diary_yr=diary_yr)

    def fetch_pdf(self, url: str) -> bytes:
        raise NotImplementedError("SC order-PDF fetch is a follow-up (separate pageid)")
