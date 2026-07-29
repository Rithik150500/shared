"""``display_pdf_new.php`` answering "order is not uploaded" is a NEGATIVE ANSWER,
not a court outage.

eCourts returns HTTP **200** with a small HTML fragment when the order row exists
in case history but NIC never uploaded the document:

    <h2><table border='0' align='center'><td align='center' style='font-color:red;'>
    order is not uploaded for - case no- CW/0024406/2025</td></h2>

That body fails ``_RESPONSE_ENVELOPE_RE``, so ``_send`` currently raises
``CourtSiteDown`` -> ``Outcome.TRIP_COURT``, which:

  * is retried 3x (``_RETRIABLE = (CourtSiteDown,)``), tripling wire traffic
    against a permanently-negative answer, and
  * opens the per-court breaker, which then blocks *real* downloadable PDFs for
    sibling orders in the same court.

Measured on prod over 7 days: 2574 wire calls (75.6% of the entire
``ECOURTS_THROTTLE`` metric) from only 173 distinct orders, and 100 of 105
per-court circuit opens -- ``hc:RJ`` and ``hc:DL`` were answering correctly the
whole time.

``PDFNotFound`` -> ``Outcome.NEUTRAL`` already exists for exactly this shape of
"the host answered, the payload disappointed us". These tests pin the contract:

  * "not uploaded" on display_pdf_new.php at 200 -> PDFNotFound, not retried,
    does not trip a breaker, and is NOT counted as a throttle
  * a real 405 on the SAME endpoint  -> still RateLimited (the throttle path is
    untouched; there are ~100 genuine ones per week)
  * the real throttle HTML at 200    -> still CourtSiteDown (over-match guard)
  * "not uploaded" on ANY other endpoint -> still CourtSiteDown (scope guard)
  * a reworded negative answer       -> still CourtSiteDown (safe degradation)
"""
from __future__ import annotations

import asyncio

import pytest
import requests

from ecourts_client import pdf as pdf_mod
from ecourts_client._session import Session
from ecourts_client.errors import CourtSiteDown, PDFNotFound, RateLimited
from ecourts_client.resilience.failure_policy import Outcome, classify_failure
from ecourts_client.resilience.retry import with_retry

from .test_session_throttle import _THROTTLE_HTML


# Captured verbatim from prod (deploy-casepilot-1, 2026-07-29). Note the malformed
# markup -- <td> outside <tr>, unclosed <table> -- which is why the tag structure is
# NOT a safe anchor to match on.
_ORDER_ABSENT_HTML = (
    "<h2><table border='0' align='center'><td align='center' "
    "style='font-color:red;'> order is not uploaded for - case no- "
    "CW/0024406/2025</td></h2>"
)

_PDF_ENDPOINT = "display_pdf_new.php"


class _CannedHTTP:
    """Stand-in for ``requests.Session`` returning a fixed status+body.

    Unlike the double in ``test_session_throttle``, this one implements
    ``.request`` as well as ``.get``: ``fetch_order_pdf`` calls
    ``_send(..., method="POST")``, which routes to ``self._http.request``.
    A ``.get``-only double would silently exercise the wrong transport path.
    """

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self._text = text
        self.calls = 0
        self.get_calls = 0
        self.request_calls = 0
        self.headers: dict[str, str] = {}

    def _resp(self) -> requests.Response:
        self.calls += 1
        resp = requests.Response()
        resp.status_code = self.status_code
        resp._content = self._text.encode("utf-8")
        return resp

    def get(self, *args, **kwargs):
        self.get_calls += 1
        return self._resp()

    def request(self, method, url, *args, **kwargs):
        self.request_calls += 1
        return self._resp()


def _sess() -> Session:
    s = Session(scope="district")
    s.jwt = "fake-jwt-for-test"  # skip bootstrap; go straight to _send
    return s


def _send_pdf(session, status=200, body=_ORDER_ABSENT_HTML, endpoint=_PDF_ENDPOINT):
    session._http = _CannedHTTP(status, body)  # type: ignore[assignment]
    return session._send(endpoint, {}, with_bearer=True, method="POST")


# --- the double itself must exercise the POST path -------------------------

def test_double_routes_post_through_request_not_get():
    """Guard on the test double: if this regresses to .get, every other test in
    this file would be exercising the wrong transport branch and pass vacuously."""
    s = _sess()
    fake = _CannedHTTP(200, _ORDER_ABSENT_HTML)
    s._http = fake  # type: ignore[assignment]
    with pytest.raises((PDFNotFound, CourtSiteDown)):
        s._send(_PDF_ENDPOINT, {}, with_bearer=True, method="POST")
    assert fake.request_calls == 1, "POST must route through .request"
    assert fake.get_calls == 0, "POST must not route through .get"


# --- the fix ---------------------------------------------------------------

def test_order_not_uploaded_is_pdfnotfound():
    """RED until the fix: a 200 'not uploaded' answer is a missing document,
    not a court outage."""
    s = _sess()
    with pytest.raises(PDFNotFound):
        _send_pdf(s)


def test_absent_pdf_is_classified_neutral_for_the_breaker():
    """The whole point of the reclassification: NEUTRAL means it does not count
    as an availability signal, so it can never open a court breaker."""
    s = _sess()
    with pytest.raises(PDFNotFound) as exc:
        _send_pdf(s)
    assert classify_failure(exc.value) is Outcome.NEUTRAL


def test_absent_pdf_is_not_retried():
    """The 3x amplification. PDFNotFound is not in _RETRIABLE, so a permanently
    negative answer must cost exactly ONE wire call, not three.

    This single assertion is the difference between 2574 and ~858 weekly requests.
    """
    s = _sess()
    fake = _CannedHTTP(200, _ORDER_ABSENT_HTML)
    s._http = fake  # type: ignore[assignment]

    @with_retry(max_attempts=3, base_delay=0.005)
    async def call_async():
        return s._send(_PDF_ENDPOINT, {}, with_bearer=True, method="POST")

    with pytest.raises(PDFNotFound):
        asyncio.run(call_async())

    assert fake.calls == 1, (
        f"absent PDF produced {fake.calls} HTTP requests; it must NOT be retried"
    )


def test_fetch_order_pdf_propagates_pdfnotfound():
    """The caller's real contract: preprocessing.py calls fetch_order_pdf, whose
    v4 branch POSTs display_pdf_new.php. It already handles PDFNotFound by
    downgrading to logger.info -- this makes reality match that handler."""
    s = _sess()
    s._http = _CannedHTTP(200, _ORDER_ABSENT_HTML)  # type: ignore[assignment]
    order_url = pdf_mod.encode_v4_order(
        {"filename": "x.pdf", "caseno": "CW/0024406/2025", "cCode": "1"}
    )
    with pytest.raises(PDFNotFound):
        pdf_mod.fetch_order_pdf(s, order_url)


def test_absent_pdf_is_not_counted_as_a_throttle(caplog):
    """``ECOURTS_THROTTLE`` is the operator's throttle tally; a 200 negative
    answer inflating it to 75.6% made the metric useless. The event should be
    countable under its own kind instead."""
    s = _sess()
    with caplog.at_level("INFO"):
        with pytest.raises(PDFNotFound):
            _send_pdf(s)
    text = caplog.text
    assert "ECOURTS_THROTTLE" not in text, (
        "a 200 'not uploaded' answer must not be logged as a throttle"
    )
    assert "pdf_not_uploaded" in text, (
        "the negative answer should still be countable under a distinct kind"
    )


# --- guards: everything that must NOT change -------------------------------

def test_405_on_display_pdf_new_is_still_ratelimited():
    """~100 genuine burst-throttle 405s hit this endpoint per week. The 405 branch
    sits ~30 lines before the envelope check, so it cannot reach the new code --
    this pins that."""
    s = _sess()
    with pytest.raises(RateLimited):
        _send_pdf(s, status=405, body=_THROTTLE_HTML)


def test_throttle_html_at_200_on_display_pdf_new_is_still_courtsitedown():
    """OVER-MATCH GUARD. The real throttle page says 'Search Page not Found here'.
    If the matcher is ever loosened to /not\\s+found/ this test fails."""
    s = _sess()
    with pytest.raises(CourtSiteDown):
        _send_pdf(s, status=200, body=_THROTTLE_HTML)


def test_not_uploaded_on_another_endpoint_is_still_courtsitedown():
    """SCOPE GUARD. display_pdf_new.php is the only endpoint whose legitimate
    negative answer is HTML. The same words from a search endpoint are a genuine
    anomaly and must stay an availability signal."""
    s = _sess()
    with pytest.raises(CourtSiteDown):
        _send_pdf(s, body=_ORDER_ABSENT_HTML, endpoint="searchByPartyName.php")


def test_reworded_negative_answer_degrades_to_courtsitedown():
    """SAFE-DEGRADATION CONTRACT, asserted deliberately rather than left to luck.

    If NIC rewords to something the matcher misses, we fall back to today's
    behaviour -- never to swallowing a real outage. The ``non_envelope`` counter
    climbing again is precisely the alarm that the wording drifted.
    """
    s = _sess()
    with pytest.raises(CourtSiteDown):
        _send_pdf(s, body="<h2>order is unavailable for this case</h2>")


def test_valid_envelope_on_pdf_endpoint_still_decrypts():
    """No regression on the success path for this endpoint."""
    from .test_session_throttle import _make_envelope

    s = _sess()
    payload = {"pdf_url": "https://csc.ecourts.gov.in/x/abc.pdf"}
    assert _send_pdf(s, body=_make_envelope(payload)) == payload
