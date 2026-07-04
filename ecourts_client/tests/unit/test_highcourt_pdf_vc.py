"""Tests for HighCourtClient.fetch_cause_list_pdf_rows_with_vc.

Downloads the HC cause-list PDF ONCE and returns (rows, vc_map) so the indexer
avoids a second fetch (throttle risk).  Network is monkeypatched.
"""
from __future__ import annotations

import pathlib

import pytest

_FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures"


def _ap_pdf_bytes() -> bytes:
    return (_FIXTURES / "hc_sample.pdf").read_bytes()


def test_fetch_cause_list_pdf_rows_with_vc_returns_tuple(monkeypatch):
    """Monkeypatch fetch_pdf so no network is hit; assert (list, dict) shape."""
    from ecourts_client.highcourt import HighCourtClient

    # The real download call in fetch_cause_list_pdf_rows is:
    #   pdf_bytes = fetch_pdf(self._session._http, pdf_url)
    # Patch the name in the highcourt module's namespace.
    monkeypatch.setattr("ecourts_client.highcourt.fetch_pdf", lambda _http, _url: _ap_pdf_bytes())

    client = HighCourtClient()
    result = client.fetch_cause_list_pdf_rows_with_vc(pdf_url="http://fake/causelist.pdf")

    assert isinstance(result, tuple), "must return a tuple"
    assert len(result) == 2, "tuple must be (rows, vc_map)"
    rows, vc_map = result
    assert isinstance(rows, list), "first element must be list[HCCauseListPDFRow]"
    assert isinstance(vc_map, dict), "second element must be dict (court_no -> VCAccess)"


def test_fetch_cause_list_pdf_rows_with_vc_single_download(monkeypatch):
    """fetch_pdf is called exactly ONCE (download-once contract)."""
    from ecourts_client.highcourt import HighCourtClient

    call_count = {"n": 0}

    def _fake_fetch_pdf(_http, _url):
        call_count["n"] += 1
        return _ap_pdf_bytes()

    monkeypatch.setattr("ecourts_client.highcourt.fetch_pdf", _fake_fetch_pdf)

    client = HighCourtClient()
    client.fetch_cause_list_pdf_rows_with_vc(pdf_url="http://fake/causelist.pdf")

    assert call_count["n"] == 1, "PDF must be downloaded exactly once"


def test_fetch_cause_list_pdf_rows_with_vc_ap_no_vc_links(monkeypatch):
    """AP HC fixture has no VC headers -> vc_map is empty dict."""
    from ecourts_client.highcourt import HighCourtClient

    monkeypatch.setattr("ecourts_client.highcourt.fetch_pdf", lambda _http, _url: _ap_pdf_bytes())

    client = HighCourtClient()
    _rows, vc_map = client.fetch_cause_list_pdf_rows_with_vc(pdf_url="http://fake/causelist.pdf")

    assert vc_map == {}, "AP HC PDF has no COURT NO. VC headers -> must return empty dict"
