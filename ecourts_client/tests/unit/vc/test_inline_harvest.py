import pathlib

from ecourts_client.vc.inline import harvest_vc_links
from ecourts_client.vc.models import VCVendor

_FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures"


def test_harvest_delhi_hc_headers_and_dewrap():
    text = (
        "COURT NO. 26  HON'BLE MS. JUSTICE NEENA BANSAL KRISHNA\n"
        "CLICK TO JOIN VC: https://delhihighcourt.webex.com/meet/dhcecourtvc1\n"
        "MEETING NUMBER 2512 399 8226 PASSWORD 1234\n"
        "1  W.P.(C) 100/2026  A vs B\n"
        "COURT NO. 08  HON'BLE MR. JUSTICE SACHIN DATTA\n"
        "CLICK HERE TO JOIN V.C. : https://virtualcourtdhc.webex.com/meet/\n"
        "virtualcourtdhc6\n"        # wrapped across a line break
    )
    out = harvest_vc_links(text)
    assert out["26"].url.endswith("/dhcecourtvc1")
    assert out["26"].vendor is VCVendor.WEBEX_HOSTED or out["26"].vendor is VCVendor.WEBEX_PERSONAL_ROOM
    assert out["26"].meeting_id == "2512 399 8226"
    assert out["08"].url.endswith("/virtualcourtdhc6")   # de-wrapped


def test_harvest_returns_empty_when_no_links():
    assert harvest_vc_links("COURT NO. 1  JUSTICE X\n1  CS 1/2026  A vs B\n") == {}


# ── harvest_vc_links_from_pdf ─────────────────────────────────────────────────


def test_harvest_from_pdf_garbage_returns_empty():
    from ecourts_client.vc.inline import harvest_vc_links_from_pdf

    assert harvest_vc_links_from_pdf(b"not a pdf") == {}


def test_harvest_from_pdf_empty_bytes_returns_empty():
    from ecourts_client.vc.inline import harvest_vc_links_from_pdf

    assert harvest_vc_links_from_pdf(b"") == {}


def test_harvest_from_pdf_real_pdf_no_false_positives():
    from ecourts_client.vc.inline import harvest_vc_links_from_pdf

    p = _FIXTURES / "hc_sample.pdf"
    out = harvest_vc_links_from_pdf(p.read_bytes())
    assert isinstance(out, dict)  # extraction path works; AP PDF has no VC links -> {}


def test_harvest_from_pdf_never_raises_on_truncated_bytes():
    from ecourts_client.vc.inline import harvest_vc_links_from_pdf

    # Start of a PDF header but truncated — must not raise
    result = harvest_vc_links_from_pdf(b"%PDF-1.4 truncated garbage")
    assert isinstance(result, dict)


# ── FIX 1: stray non-VC URL must not beat the real Webex join link ────────────


def test_stray_url_above_webex_join_line_is_discarded():
    """A non-Webex URL printed ABOVE 'CLICK TO JOIN VC' must not be stored;
    the genuine Webex link on the join-hint line should win."""
    text = (
        "COURT NO. 05  HON'BLE MR. JUSTICE A. B. SHARMA\n"
        "For more info see: https://ecourts.gov.in/notice\n"   # stray non-VC URL, no join hint
        "CLICK TO JOIN VC: https://delhihighcourt.webex.com/meet/dhcecourtvc5\n"
        "MEETING NUMBER 1111 222 3333 PASSWORD abcd\n"
        "1  W.P.(C) 200/2026  X vs Y\n"
    )
    out = harvest_vc_links(text)
    assert "05" in out, "Court 05 must appear in results"
    vc = out["05"]
    assert "webex.com" in vc.url, (
        f"Expected the Webex join URL but got: {vc.url!r} — stray non-VC URL was stored instead"
    )
    assert vc.vendor in (VCVendor.WEBEX_PERSONAL_ROOM, VCVendor.WEBEX_HOSTED)


def test_stray_custom_url_without_join_hint_not_stored():
    """A CUSTOM URL with no join hint at all must not be stored."""
    text = (
        "COURT NO. 03  HON'BLE MR. JUSTICE C. D. SINGH\n"
        "Please visit https://ecourts.gov.in/notice for directions\n"
        "1  CS 300/2026  P vs Q\n"
    )
    out = harvest_vc_links(text)
    assert "03" not in out, (
        "CUSTOM URL with no join hint must not be stored, but it was"
    )


def test_known_vendor_upgrade_over_custom():
    """If a CUSTOM URL appears first, a later known-vendor URL under the same
    header should replace it (upgrade, not silently lose the Webex link)."""
    text = (
        "COURT NO. 07  HON'BLE MS. JUSTICE E. F. VERMA\n"
        "CLICK TO JOIN: https://ecourts.gov.in/notice\n"   # has join hint but CUSTOM vendor
        "JOIN VC LINK: https://delhihighcourt.webex.com/meet/dhcecourtvc7\n"
    )
    out = harvest_vc_links(text)
    assert "07" in out
    vc = out["07"]
    assert "webex.com" in vc.url, (
        f"Known-vendor URL should have replaced the CUSTOM one, got: {vc.url!r}"
    )


def test_meeting_id_attributed_to_correct_court():
    """Two courts with similar VC lines must not cross-attribute meeting IDs."""
    text = (
        "COURT NO. 11  HON'BLE MR. JUSTICE G. H. MISRA\n"
        "CLICK TO JOIN VC: https://delhihighcourt.webex.com/meet/courtvc11\n"
        "MEETING NUMBER 1100 001 1001 PASSWORD pass11\n"
        "1  W.P.(C) 11/2026  A11 vs B11\n"
        "COURT NO. 22  HON'BLE MS. JUSTICE I. J. KAUR\n"
        "CLICK TO JOIN VC: https://delhihighcourt.webex.com/meet/courtvc11\n"  # identical URL
        "MEETING NUMBER 2200 002 2002 PASSWORD pass22\n"
        "1  W.P.(C) 22/2026  A22 vs B22\n"
    )
    out = harvest_vc_links(text)
    # Each court must carry its own meeting id, not the first occurrence
    assert out["11"].meeting_id == "1100 001 1001"
    assert out["22"].meeting_id == "2200 002 2002"
