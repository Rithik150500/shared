"""``Invalid Input4`` is a rejected request, not a court outage.

Prod 2026-07-31, reproduced live against Delhi HC case DLHC010320362026
(``C.A.(COMM.IPD-TM) 49/2026``, order dated 2026-07-23)::

    ECOURTS_THROTTLE kind=non_envelope endpoint=display_pdf_new.php http=200
    PDF FAIL CourtSiteDown "non-envelope response from display_pdf_new.php
                            (HTTP 200, 14 bytes): 'Invalid Input4'"

Three things were wrong with calling that ``CourtSiteDown``:

* ``_RETRIABLE = (CourtSiteDown,)`` -- we re-sent byte-identical parameters
  three times. A parameter-validation rejection cannot succeed on retry.
* ``failure_policy`` maps ``CourtSiteDown`` to ``TRIP_COURT``, so a request we
  malformed counted against ``hc:DL``'s availability and could open a breaker
  in front of orders that download perfectly well.
* It was filed under ``kind=non_envelope`` in the throttle metric, hiding a
  request defect inside an availability statistic.

This does NOT widen ``_PDF_ABSENT_RE``. Folding ``Invalid Input4`` into
``PDFNotFound`` would recast a malformed request as a benign "no such document"
and make it permanently invisible -- we would silently stop fetching PDFs that
exist. ``PDFRequestRejected`` is deliberately a distinct, loud, non-retriable
error that keeps the defect on the record.

Note the order row that produced this carried ALL SEVEN ``_V4_ORDER_FIELDS``
populated (``appFlag='1'``, ``state_cd='26'``, ``dist_cd='1'``), so the
2026-07-30 ``ECOURTS_ORDER_FIELDS_MISSING`` diagnostic does not fire here and
the blank-field hypothesis is not the cause. What NIC objects to is still open;
this change stops us from mislabelling it while we find out.
"""
from __future__ import annotations

import pytest

from ecourts_client._session import Session
from ecourts_client.errors import CourtSiteDown, PDFNotFound, PDFRequestRejected
from ecourts_client.resilience.failure_policy import Outcome, classify_failure
from ecourts_client.resilience.retry import _RETRIABLE

from .test_pdf_absent_not_outage import _CannedHTTP, _ORDER_ABSENT_HTML
from .test_session_throttle import _THROTTLE_HTML


_PDF_ENDPOINT = "display_pdf_new.php"

# Verbatim prod body: HTTP 200, exactly 14 bytes.
_REJECTED_BODY = "Invalid Input4"


def _sess() -> Session:
    s = Session(scope="highcourt")
    s.jwt = "fake-jwt-for-test"  # skip bootstrap; go straight to _send
    return s


def _send_pdf(status=200, body=_REJECTED_BODY, endpoint=_PDF_ENDPOINT):
    s = _sess()
    s._http = _CannedHTTP(status, body)  # type: ignore[assignment]
    return s._send(endpoint, {}, with_bearer=True, method="POST")


def test_body_is_exactly_the_14_bytes_we_saw():
    """Guard the fixture: the whole diagnosis rests on this being a terse,
    indexed validation error rather than a content answer."""
    assert len(_REJECTED_BODY) == 14


# --- the fix ---------------------------------------------------------------

def test_invalid_input_is_rejected_not_outage():
    with pytest.raises(PDFRequestRejected) as ei:
        _send_pdf()
    # The digit is the only clue to which parameter NIC objected to, so it must
    # survive into the message.
    assert "Invalid Input4" in str(ei.value)


def test_rejected_is_not_a_subclass_of_court_site_down():
    """If it were, every retry/breaker guard below would pass vacuously."""
    assert not issubclass(PDFRequestRejected, CourtSiteDown)


def test_not_retriable():
    assert not issubclass(PDFRequestRejected, tuple(_RETRIABLE))


def test_does_not_trip_a_breaker():
    assert classify_failure(PDFRequestRejected("x")) is Outcome.NEUTRAL


# --- narrowness guards -----------------------------------------------------

def test_absent_document_still_maps_to_pdf_not_found():
    """The 2026-07-29 fix must not regress."""
    with pytest.raises(PDFNotFound):
        _send_pdf(body=_ORDER_ABSENT_HTML)


def test_throttle_page_still_an_outage():
    """The throttle page says 'not Found', not 'Invalid Input'."""
    with pytest.raises(CourtSiteDown):
        _send_pdf(body=_THROTTLE_HTML)


def test_unrelated_html_still_an_outage():
    with pytest.raises(CourtSiteDown):
        _send_pdf(body="<html>maintenance</html>")


def test_only_applies_to_the_pdf_endpoint():
    """A terse 'Invalid Input' elsewhere is uncharacterised; leave it alone."""
    with pytest.raises(CourtSiteDown):
        _send_pdf(endpoint="caseHistoryWebService.php")


def test_non_200_is_never_downgraded():
    with pytest.raises(Exception) as ei:
        _send_pdf(status=503)
    assert not isinstance(ei.value, PDFRequestRejected)


def test_court_site_down_still_trips_the_breaker():
    """Contrast guard -- real outages were not neutered."""
    assert classify_failure(CourtSiteDown("x")) is Outcome.TRIP_COURT
