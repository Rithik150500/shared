from datetime import date

from ecourts_client.vc.hc_website import (
    DelhiHCVCSource, HCWebsiteVCSource, get_hc_vc_source,
)


def test_matches_date_day_month_and_monthdir():
    s = DelhiHCVCSource()
    m = s._matches_date
    assert m("/files/2026-07/cause-list/sup_2_06.07.pdf", date(2026, 7, 6))
    assert m("/files/2026-07/cause-list/final_suppl.06.07.2026.pdf", date(2026, 7, 6))
    assert m("/files/2026-07/cause-list/vk_sup_fnl_for_06.07.26.pdf", date(2026, 7, 6))
    assert not m("/files/2026-07/cause-list/sup_2_07.07.pdf", date(2026, 7, 6))   # wrong day
    assert not m("/files/2026-06/cause-list/sup_2_06.06.pdf", date(2026, 7, 6))   # wrong month dir


def test_index_pdf_links_extracts_and_dedupes():
    class _Resp:
        status_code = 200
        text = (
            'x <a href="/files/2026-07/cause-list/sup_2_06.07.pdf">a</a> '
            '<a href="/files/2026-07/cause-list/sup_2_06.07.pdf">dup</a> '
            '<a href="/files/2026-07/cause-list/adv_06.07.pdf">b</a> '
            'noise /files/other/nope.txt'
        )

        def raise_for_status(self):
            pass

    class _Sess:
        def get(self, *a, **k):
            return _Resp()

    s = DelhiHCVCSource(session=_Sess())
    links = s._index_pdf_links()
    assert links == [
        "/files/2026-07/cause-list/adv_06.07.pdf",
        "/files/2026-07/cause-list/sup_2_06.07.pdf",
    ]


def test_registry_resolves_delhi_and_protocol():
    src = get_hc_vc_source("DL")
    assert isinstance(src, DelhiHCVCSource)
    assert isinstance(src, HCWebsiteVCSource)   # satisfies the runtime protocol
    assert get_hc_vc_source("dl") is not None    # case-insensitive
    assert get_hc_vc_source("XX") is None


def test_index_failure_is_fail_open():
    class _Sess:
        def get(self, *a, **k):
            raise RuntimeError("network down")

    s = DelhiHCVCSource(session=_Sess())
    assert s.vc_links_for_date(date(2026, 7, 6)) == {}
