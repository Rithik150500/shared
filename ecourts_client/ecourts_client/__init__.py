"""Public API. Anything not re-exported here is internal."""
from ecourts_client.client import (
    ECourtsClient,
    fetch_case,
    fetch_pdf,
    get_client_for,
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
    JWTExpired,
    PDFInvalid,
    PDFNotFound,
    RateLimited,
    SchemaChanged,
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

__version__ = "0.1.0"

__all__ = [
    "Act", "BenchRef", "BlockedByGeoIP", "CNRMalformed", "CNRNotFound",
    "Case", "CaseStub", "CaseTypeRef", "CategoryDetails", "CauseList",
    "CauseListEntry", "CircuitOpen", "CourtComplexRef", "CourtSiteDown",
    "DailyBusiness", "DistrictCourtClient", "DistrictRef", "ECourtsClient",
    "ECourtsConfig", "ECourtsError", "EcourtsError", "FIRDetails",
    "HCBenchSitting", "HCCauseListIndex", "HCCauseListPDFRow",
    "HearingHistoryRow", "HighCourtClient", "JWTExpired", "ObjectionDetails",
    "OrderRef", "PDFInvalid", "PDFNotFound", "Party", "PoliceStationRef",
    "RateLimited", "SchemaChanged", "StateRef", "fetch_case", "fetch_pdf",
    "get_client_for",
]
