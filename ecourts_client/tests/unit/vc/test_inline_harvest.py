from ecourts_client.vc.inline import harvest_vc_links
from ecourts_client.vc.models import VCVendor


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
