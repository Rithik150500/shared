"""Forum taxonomy + the capability-flagged adapter contract.

This is the multi-forum superset of the original eCourts-only client. The
``Forum`` values are the SINGLE canonical namespace used everywhere — they match
the ``cases.forum`` DB column (data-access) verbatim, so no translation is
needed between the DB, the adapter registry, the API and the UI.

New forums are additive: implement ``ForumAdapter`` (a structural superset of the
legacy ``ECourtsClient`` Protocol) and register a factory via
``client.register_adapter``. Forums with no registered adapter are "manual"
(handled by the manual-entry path, never auto-refreshed).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from ecourts_client.models import Case


class Forum(str, Enum):
    """Case forums. Values are identical to the ``cases.forum`` DB discriminator."""

    ECOURTS_DISTRICT = "ecourts_district"
    ECOURTS_HIGHCOURT = "ecourts_highcourt"
    SUPREME_COURT = "supreme_court"
    CONSUMER = "consumer"
    DRT = "drt"                          # LEGACY (grandfathered one release; folded into TRIBUNAL/kind=drt)
    ARBITRATION = "arbitration"
    TRIBUNAL = "tribunal"                # generic tribunals family; sub-typed by TribunalKind


class TribunalKind(str, Enum):
    """The specific tribunal within the generic ``tribunal`` forum.

    Values are the ``cases.tribunal_kind`` DB discriminator (only set when
    ``forum='tribunal'``). Additive: a new tribunal is a new member here — no new
    ``Forum`` value and no schema migration (the CHECK deliberately enumerates
    nothing; validation is in code). DRT/DRAT live here (the standalone
    ``Forum.DRT`` is grandfathered one release, then retired)."""

    NCLT = "nclt"      # National Company Law Tribunal
    NCLAT = "nclat"    # NCL Appellate Tribunal
    CAT = "cat"        # Central Administrative Tribunal
    ITAT = "itat"      # Income Tax Appellate Tribunal
    NGT = "ngt"        # National Green Tribunal
    TDSAT = "tdsat"    # Telecom Disputes Settlement & Appellate Tribunal
    AFT = "aft"        # Armed Forces Tribunal
    CESTAT = "cestat"  # Customs Excise & Service Tax Appellate Tribunal
    DRT = "drt"        # Debt Recovery Tribunal
    DRAT = "drat"      # Debt Recovery Appellate Tribunal
    SAT = "sat"        # Securities Appellate Tribunal
    LABOUR_COURT = "labour_court"                # State Labour Court (ID Act §2A) — manual, no online source
    INDUSTRIAL_TRIBUNAL = "industrial_tribunal"  # State Industrial Tribunal-cum-Labour Court (ID Act §33A/§33(2B)) — manual


class IdentifierKind(str, Enum):
    """How a case is identified within a forum."""

    CNR = "cnr"                          # 16-char eCourts CNR
    DIARY_NUMBER = "diary_number"        # Supreme Court diary/SLP number
    EJAGRITI_CASE_NO = "ejagriti_case_no"  # e-Jagriti (old Confonet vs new) + tier
    DRT_CASE_NO = "drt_case_no"          # legacy DRT forum (see Forum.DRT)
    TRIBUNAL_CASE_NO = "tribunal_case_no"  # generic tribunal case no (per-kind adapter refines)
    MANUAL = "manual"                    # opaque, user-supplied


# The eCourts family — the only forums that carry a 16-char CNR.
ECOURTS_FORUMS: frozenset[Forum] = frozenset(
    {Forum.ECOURTS_DISTRICT, Forum.ECOURTS_HIGHCOURT}
)

# All tribunal sub-types (for validation + iteration).
TRIBUNAL_KINDS: frozenset[TribunalKind] = frozenset(TribunalKind)

# Which identifier kind each forum expects. Used by routing.validate_identifier.
FORUM_IDENTIFIER_KIND: dict[Forum, IdentifierKind] = {
    Forum.ECOURTS_DISTRICT: IdentifierKind.CNR,
    Forum.ECOURTS_HIGHCOURT: IdentifierKind.CNR,
    Forum.SUPREME_COURT: IdentifierKind.DIARY_NUMBER,
    Forum.CONSUMER: IdentifierKind.EJAGRITI_CASE_NO,
    Forum.DRT: IdentifierKind.DRT_CASE_NO,
    Forum.ARBITRATION: IdentifierKind.MANUAL,
    Forum.TRIBUNAL: IdentifierKind.TRIBUNAL_CASE_NO,
}


@dataclass(frozen=True)
class ForumCapabilities:
    """What an adapter can do, so callers can branch without hard-coding forums."""

    forum: Forum
    identifier_kind: IdentifierKind
    supports_fetch: bool   # can fetch a Case from an identifier
    supports_search: bool  # party-name / case-number search
    supports_pdf: bool     # can download order/judgment PDFs
    is_manual: bool         # no automated transport (arbitration; pre-automation)
    # Set only for tribunal adapters (forum=TRIBUNAL); identifies which kind this
    # adapter serves. None for all single-forum adapters. Keyword/default so
    # every existing ForumCapabilities(...) construction is unaffected.
    tribunal_kind: "TribunalKind | None" = None


@runtime_checkable
class ForumAdapter(Protocol):
    """Structural contract for a per-forum adapter.

    A superset of the legacy ``ECourtsClient`` Protocol: DistrictCourtClient /
    HighCourtClient satisfy both (they gained a ``capabilities`` class attr and
    already have ``fetch_case`` / ``fetch_pdf``).
    """

    capabilities: ForumCapabilities

    def fetch_case(self, identifier: str) -> Case: ...
    def fetch_pdf(self, url: str) -> bytes: ...
