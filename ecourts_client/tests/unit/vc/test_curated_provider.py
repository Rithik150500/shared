from ecourts_client.vc.curated import CuratedMapProvider
from ecourts_client.vc.models import make_key, VCVendor

ROWS = [
    {"scope": "district", "complex": "DLND01", "court_no": "75",
     "designation": "District Judge-04", "vendor": "webex_personal_room",
     "url": "https://districtcourtdelhi.webex.com/meet/dj04-nd", "room": "137A"},
]


def test_curated_hit_by_court_no_and_designation_alias():
    p = CuratedMapProvider.from_rows(ROWS)
    a = p.resolve(make_key("district", "DLND01", "75"))
    assert a is not None and a.vendor is VCVendor.WEBEX_PERSONAL_ROOM
    assert a.url.endswith("/dj04-nd")
    # designation alias
    assert p.resolve(("district", "dlnd01", "desg:district judge-04")) is not None
    # miss
    assert p.resolve(make_key("district", "DLND01", "999")) is None


def test_malformed_row_is_skipped_not_raised():
    p = CuratedMapProvider.from_rows([{"garbage": 1}, ROWS[0]])
    assert p.resolve(make_key("district", "DLND01", "75")) is not None
