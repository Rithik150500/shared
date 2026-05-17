from __future__ import annotations


class ECourtsError(Exception):
    """Base class for all eCourts data-access layer errors."""


class CNRMalformed(ECourtsError):
    """The supplied CNR doesn't match the 16-char regex or has an unknown state code."""

    def __init__(self, cnr: str, reason: str | None = None) -> None:
        self.cnr = cnr
        self.reason = reason
        super().__init__(f"Malformed CNR: {cnr}" + (f" ({reason})" if reason else ""))


class CNRNotFound(ECourtsError):
    """eCourts returned a 'no record found' response for this CNR."""

    def __init__(self, cnr: str) -> None:
        self.cnr = cnr
        super().__init__(f"CNR not found: {cnr}")


class CourtSiteDown(ECourtsError):
    """eCourts returned a 5xx, timed out, or showed a maintenance banner."""


class RateLimited(ECourtsError):
    """eCourts returned a throttle response (429 or known throttle HTML)."""


class BlockedByGeoIP(ECourtsError):
    """eCourts returned a 403 with the GeoIP-block signature."""


class PDFNotFound(ECourtsError):
    """The PDF URL returned 404 or a non-PDF body."""


class PDFInvalid(ECourtsError):
    """The PDF bytes downloaded but failed validation (truncated, non-PDF magic)."""


class SchemaChanged(ECourtsError):
    """A parser couldn't find an expected field — likely NIC schema drift."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"Schema changed at field '{field}': {reason}")


# Spec-name alias (some consumer code in earlier docs uses `EcourtsError`).
EcourtsError = ECourtsError


class CircuitOpen(ECourtsError):
    """Circuit breaker is currently open; the request was rejected without hitting eCourts."""

    def __init__(self, name: str, retry_after_seconds: float) -> None:
        self.name = name
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Circuit '{name}' is open; retry after {retry_after_seconds:.1f}s")


class JWTExpired(ECourtsError):
    """Two consecutive 401s; the caller must mint a fresh JWT."""
