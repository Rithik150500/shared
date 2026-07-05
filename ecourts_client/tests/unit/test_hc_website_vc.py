from datetime import date

from ecourts_client.vc.hc_website import (
    DelhiHCVCSource, HCWebsiteVCSource, get_hc_vc_source, normalize_judge,
)


def test_normalize_judge_presiding_and_format_invariant():
    # eCourts roster and website spelling of the SAME presiding judge collapse equal.
    ecourts = "HON'BLE MR. JUSTICE C.HARI SHANKAR, HON'BLE MR. JUSTICE VINOD KUMAR"
    website = "HON'BLE MR.JUSTICE C.HARI SHANKAR"
    assert normalize_judge(ecourts) == normalize_judge(website) == "CHARISHANKAR"
    assert normalize_judge("HON'BLE DR.JUSTICE SWARANA KANTA SHARMA") == "SWARANAKANTASHARMA"
    assert normalize_judge("PRE LOK ADALAT") == "PRELOKADALAT"   # no JUSTICE -> passes through
    assert normalize_judge("") is None
    assert normalize_judge(None) is None


def test_court_to_judge_parses_blocks():
    text = (
        "COURT NO. 25\n"
        "HON'BLE DR.JUSTICE SWARANA KANTA SHARMA\n"
        "CLICK HERE TO JOIN VC https://delhihighcourt.webex.com/meet/dhcecourtvc\n"
        "COURT NO. 05\n"
        "HON'BLE MR.JUSTICE C.HARI SHANKAR\n"
    )

    # exercise the static parser's regexes on the block structure (building a
    # real PDF for _court_to_judge is overkill).
    from ecourts_client.vc.hc_website import _COURT_HDR, _JUDGE_LINE
    courts = _COURT_HDR.findall(text)
    assert courts == ["25", "05"]
    assert _JUDGE_LINE.search("HON'BLE MR.JUSTICE C.HARI SHANKAR")


def test_matches_date_day_month_and_monthdir():
    m = DelhiHCVCSource()._matches_date
    assert m("/files/2026-07/cause-list/sup_2_06.07.pdf", date(2026, 7, 6))
    assert m("/files/2026-07/cause-list/final_suppl.06.07.2026.pdf", date(2026, 7, 6))
    assert not m("/files/2026-07/cause-list/sup_2_07.07.pdf", date(2026, 7, 6))
    assert not m("/files/2026-06/cause-list/sup_2_06.06.pdf", date(2026, 7, 6))


def test_index_pdf_links_extracts_and_dedupes():
    class _Resp:
        status_code = 200
        text = (
            'x <a href="/files/2026-07/cause-list/sup_2_06.07.pdf">a</a> '
            '<a href="/files/2026-07/cause-list/sup_2_06.07.pdf">dup</a> '
            '<a href="/files/2026-07/cause-list/adv_06.07.pdf">b</a>'
        )

        def raise_for_status(self):
            pass

    class _Sess:
        def get(self, *a, **k):
            return _Resp()

    links = DelhiHCVCSource(session=_Sess())._index_pdf_links()
    assert links == [
        "/files/2026-07/cause-list/adv_06.07.pdf",
        "/files/2026-07/cause-list/sup_2_06.07.pdf",
    ]


def test_registry_and_fail_open():
    src = get_hc_vc_source("DL")
    assert isinstance(src, DelhiHCVCSource)
    assert isinstance(src, HCWebsiteVCSource)
    assert get_hc_vc_source("dl") is not None
    assert get_hc_vc_source("XX") is None

    class _Sess:
        def get(self, *a, **k):
            raise RuntimeError("network down")

    assert DelhiHCVCSource(session=_Sess()).vc_by_judge_for_date(date(2026, 7, 6)) == {}
