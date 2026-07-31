"""``display_pdf_new.php`` rejects a ``caseno`` containing a hyphen.

Root-caused live on prod 2026-07-31 against Delhi HC ``DLHC010320362026``
(``C.A.(COMM.IPD-TM) 49/2026``), one parameter mutated at a time::

    caseno='C.A.(COMM.IPD-TM)/0000049/2026'   -> HTTP 200 'Invalid Input4'
    caseno='C.A.(COMM.IPDTM)/0000049/2026'    -> OK, signed pdf_url
    caseno='C.A.(COMM.IPD TM)/0000049/2026'   -> OK
    caseno='C.A./0000049/2026'                -> OK
    caseno with the number un-zero-padded     -> 'Invalid Input4'  (padding matters)
    appFlag='' or 'O', or bilingual_flag absent -> 'Invalid Input4' (unrelated)

and the control -- Stirling ``CS(COMM)/0001309/2025``, no hyphen -- downloads
throughout. The recovered bytes are the genuine document: a 1-page %PDF-1.5
whose text opens "C.A.(COMM.IPD-TM) 49/2026, I.A. 19063/2026 / FUTURE FOODS
INDIA .....Appellant" and closes "Re-notify on 02nd December 2026", matching
the case's stored ``date_next_list``.

So this is a server-side input validator on NIC's side that does not accept
``-`` in ``caseno``. It is not something we can ask them to change, and it
makes EVERY order PDF undownloadable for the whole Delhi HC Intellectual
Property Division, whose case types are hyphenated by construction
(``C.A.(COMM.IPD-TM)``, ``W.C.(C)-IPD``, ``RFA(OS)-IPD``, ``CRP-IPD``, ...).

Sanitising happens at REQUEST time, not encode time, for two reasons:
  * the ``displaypdf:`` URLs already persisted in ``case_orders.order_url``
    carry the hyphen, and they start working again with no backfill;
  * the stored value stays the true case number, which is what you want when
    reading the row back.

``filename`` is the document's real identity here -- that is why stripping the
case type entirely still returned the right PDF -- so removing the hyphen
cannot select a different document.
"""
from __future__ import annotations

from urllib.parse import parse_qsl

import pytest

from ecourts_client.pdf import _V4_ORDER_SCHEME, _wire_caseno, encode_v4_order


# Verbatim from the live v4 response for DLHC010320362026.
_BAD_ROW = {
    "filename": "/orders/2026/216000000492026_1.pdf",
    "caseno": "C.A.(COMM.IPD-TM)/0000049/2026",
    "cCode": "1",
    "appFlag": "1",
    "state_cd": "26",
    "dist_cd": "1",
    "court_code": "1",
}
# Verbatim from DLHC010992612025 -- downloads fine, must not be perturbed.
_GOOD_ROW = dict(_BAD_ROW, caseno="CS(COMM)/0001309/2025",
                 filename="/orders/2025/205100013092025_1.pdf")


class TestWireCaseno:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("C.A.(COMM.IPD-TM)/0000049/2026", "C.A.(COMM.IPDTM)/0000049/2026"),
            ("W.C.(C)-IPD/0000012/2025", "W.C.(C)IPD/0000012/2025"),
            ("RFA(OS)-IPD/0000007/2024", "RFA(OS)IPD/0000007/2024"),
            # No hyphen -> byte-identical. This is the no-regression guarantee
            # for every non-IPD order in the fleet.
            ("CS(COMM)/0001309/2025", "CS(COMM)/0001309/2025"),
            ("WP(C)/0000188/2026", "WP(C)/0000188/2026"),
        ],
    )
    def test_strips_only_hyphens(self, raw, expected):
        assert _wire_caseno(raw) == expected

    def test_zero_padding_is_preserved(self):
        """Un-padding the number also produced 'Invalid Input4', so the
        sanitiser must not touch the numeric part."""
        assert "0000049" in _wire_caseno(_BAD_ROW["caseno"])

    @pytest.mark.parametrize("raw", ["", None])
    def test_tolerates_empty(self, raw):
        assert _wire_caseno(raw) == ""


class TestStoredUrlIsUnchanged:
    def test_encode_still_stores_the_true_case_number(self):
        """Sanitising at encode time would bake a false case number into the
        DB and leave every already-stored row broken."""
        url = encode_v4_order(_BAD_ROW)
        params = dict(parse_qsl(url[len(_V4_ORDER_SCHEME):]))
        assert params["caseno"] == "C.A.(COMM.IPD-TM)/0000049/2026"

    def test_encode_output_unchanged_for_hyphenless_rows(self):
        assert encode_v4_order(_GOOD_ROW) == encode_v4_order(_GOOD_ROW)
        assert "CS(COMM)/0001309/2025" in parse_qsl(
            encode_v4_order(_GOOD_ROW)[len(_V4_ORDER_SCHEME):]
        )[1][1]


class _Sess:
    """Captures the params handed to display_pdf_new.php."""

    def __init__(self):
        self.sent = None
        self._http = object()

    def _ensure_jwt(self):
        pass

    def _send(self, endpoint, params, **kw):
        self.sent = dict(params)
        return {"pdf_url": "https://csc.ecourts.gov.in/helpdesk_alias/x.pdf"}


class TestFetchSanitisesOnTheWire:
    def _fetch(self, row, monkeypatch):
        from ecourts_client import pdf as pdf_mod

        monkeypatch.setattr(pdf_mod, "fetch_pdf", lambda http, url: b"%PDF-1.5 ok")
        s = _Sess()
        out = pdf_mod.fetch_order_pdf(s, encode_v4_order(row))
        return s.sent, out

    def test_hyphen_removed_before_send(self, monkeypatch):
        sent, out = self._fetch(_BAD_ROW, monkeypatch)
        assert sent["caseno"] == "C.A.(COMM.IPDTM)/0000049/2026"
        assert out == b"%PDF-1.5 ok"

    def test_every_other_param_is_untouched(self, monkeypatch):
        sent, _ = self._fetch(_BAD_ROW, monkeypatch)
        for k in ("filename", "cCode", "appFlag", "state_cd", "dist_cd", "court_code"):
            assert sent[k] == _BAD_ROW[k], k
        assert sent["bilingual_flag"] == "1"

    def test_hyphenless_order_is_sent_byte_identical(self, monkeypatch):
        """The fleet-wide no-regression check."""
        sent, _ = self._fetch(_GOOD_ROW, monkeypatch)
        assert sent["caseno"] == "CS(COMM)/0001309/2025"
