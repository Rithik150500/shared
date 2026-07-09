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
    DRT = "drt"
    ARBITRATION = "arbitration"


class IdentifierKind(str, Enum):
    """How a case is identified within a forum."""

    CNR = "cnr"                          # 16-char eCourts CNR
    DIARY_NUMBER = "diary_number"        # Supreme Court diary/SLP number
    EJAGRITI_CASE_NO = "ejagriti_case_no"  # e-Jagriti (old Confonet vs new) + tier
    DRT_CASE_NO = "drt_case_no"          # tribunal + case type + number + year
    MANUAL = "manual"                    # opaque, user-supplied


# The eCourts family — the only forums that carry a 16-char CNR.
ECOURTS_FORUMS: frozenset[Forum] = frozenset(
    {Forum.ECOURTS_DISTRICT, Forum.ECOURTS_HIGHCOURT}
)

# Which identifier kind each forum expects. Used by routing.validate_identifier.
FORUM_IDENTIFIER_KIND: dict[Forum, IdentifierKind] = {
    Forum.ECOURTS_DISTRICT: IdentifierKind.CNR,
    Forum.ECOURTS_HIGHCOURT: IdentifierKind.CNR,
    Forum.SUPREME_COURT: IdentifierKind.DIARY_NUMBER,
    Forum.CONSUMER: IdentifierKind.EJAGRITI_CASE_NO,
    Forum.DRT: IdentifierKind.DRT_CASE_NO,
    Forum.ARBITRATION: IdentifierKind.MANUAL,
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
