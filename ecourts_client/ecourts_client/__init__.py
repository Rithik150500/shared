"""Public API. Anything not re-exported here is internal."""
from ecourts_client.client import (
    ECourtsClient,
    fetch_case,
    fetch_case_for_forum,
    fetch_pdf,
    get_adapter,
    get_client_for,
    has_automated_adapter,
    register_adapter,
)
from ecourts_client.district import DistrictCourtClient
from ecourts_client.errors import (
    BlockedByGeoIP,
    CircuitOpen,
    CNRMalformed,
    CNRNotFound,
    CourtSiteDown,
    ECourtsError,
    EcourtsError,
    ForumNotAutomated,
    IdentifierMalformed,
    JWTExpired,
    PDFInvalid,
    PDFNotFound,
    RateLimited,
    SchemaChanged,
)
from ecourts_client.forums import (
    Forum,
    ForumAdapter,
    ForumCapabilities,
    IdentifierKind,
    TribunalKind,
)
from ecourts_client.highcourt import HighCourtClient
from ecourts_client.models import (
    Act,
    BenchRef,
    Case,
    CaseStub,
    CaseTypeRef,
    CategoryDetails,
    CauseList,
    CauseListEntry,
    CourtComplexRef,
    DailyBusiness,
    DistrictRef,
    FIRDetails,
    HCBenchSitting,
    HCCauseListIndex,
    HCCauseListPDFRow,
    HearingHistoryRow,
    ObjectionDetails,
    OrderRef,
    Party,
    PoliceStationRef,
    StateRef,
)

from ecourts_client.config import ECourtsConfig  # noqa: E402
from ecourts_client.resilience.metrics import setup_sentry_tag  # noqa: E402

setup_sentry_tag()

from ecourts_client._resilience_apply import apply_sync_resilience  # noqa: E402
apply_sync_resilience()

# Register the eCourts adapters so the forum-first path (get_adapter /
# fetch_case_for_forum) resolves them. Non-eCourts forums register in their own
# phase; until then has_automated_adapter() is False and callers use the manual
# path. The legacy fetch_case(cnr) / get_client_for path is unaffected.
register_adapter(Forum.ECOURTS_DISTRICT, DistrictCourtClient)
register_adapter(Forum.ECOURTS_HIGHCOURT, HighCourtClient)

# Consumer forum (e-Jagriti) — Phase 2. Imported after the base adapters;
# ConsumerClient satisfies the ForumAdapter protocol and registers here so the
# forum-first path (get_adapter / fetch_case_for_forum) resolves it.
from ecourts_client.consumer import ConsumerClient  # noqa: E402
register_adapter(Forum.CONSUMER, ConsumerClient)

# Supreme Court — Phase 3. Token-gated mobile backend (scourtapp.sci.gov.in);
# needs SC_MOBILE_TOKEN (a device-captured session token). Registers here so
# has_automated_adapter(SUPREME_COURT) flips true + the forum-first path resolves.
from ecourts_client.supreme import SupremeCourtClient  # noqa: E402
register_adapter(Forum.SUPREME_COURT, SupremeCourtClient)

# Tribunals family — Phase T3, per-kind under Forum.TRIBUNAL. Wave-0 (no India
# proxy, captcha-free) ships NCLAT first; each kind registers under its own
# (Forum.TRIBUNAL, kind) key so has_automated_adapter(TRIBUNAL, kind=…) flips
# true only for shipped kinds (all others stay 'manual'). See
# docs/spike-tribunal-transport.md.
from ecourts_client.tribunal import CATClient, DRTClient, NCLATClient, TDSATClient  # noqa: E402
register_adapter(Forum.TRIBUNAL, NCLATClient, kind=TribunalKind.NCLAT)
register_adapter(Forum.TRIBUNAL, TDSATClient, kind=TribunalKind.TDSAT)
register_adapter(Forum.TRIBUNAL, CATClient, kind=TribunalKind.CAT)
# One client serves both DRT + DRAT (identical transport; sc/case-type ride the identifier).
register_adapter(Forum.TRIBUNAL, DRTClient, kind=TribunalKind.DRT)
register_adapter(Forum.TRIBUNAL, DRTClient, kind=TribunalKind.DRAT)

__version__ = "0.1.0"

__all__ = [
    "Act", "BenchRef", "BlockedByGeoIP", "CNRMalformed", "CNRNotFound",
    "Case", "CaseStub", "CaseTypeRef", "CategoryDetails", "CauseList",
    "CauseListEntry", "CircuitOpen", "ConsumerClient", "CourtComplexRef", "CourtSiteDown",
    "CATClient", "DRTClient", "DailyBusiness", "DistrictCourtClient", "DistrictRef", "ECourtsClient",
    "ECourtsConfig", "ECourtsError", "EcourtsError", "FIRDetails",
    "Forum", "ForumAdapter", "ForumCapabilities", "ForumNotAutomated",
    "HCBenchSitting", "HCCauseListIndex", "HCCauseListPDFRow",
    "HearingHistoryRow", "HighCourtClient", "IdentifierKind",
    "IdentifierMalformed", "JWTExpired", "NCLATClient", "ObjectionDetails",
    "TDSATClient",
    "OrderRef", "PDFInvalid", "PDFNotFound", "Party", "PoliceStationRef",
    "RateLimited", "SchemaChanged", "StateRef", "SupremeCourtClient",
    "TribunalKind", "fetch_case",
    "fetch_case_for_forum", "fetch_pdf", "get_adapter", "get_client_for",
    "has_automated_adapter", "register_adapter",
]
