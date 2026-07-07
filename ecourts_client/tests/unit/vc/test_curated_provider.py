from ecourts_client.vc.curated import CuratedMapProvider
from ecourts_client.vc.models import make_key, VCVendor, normalize_designation

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
    # designation alias (raw desg key)
    assert p.resolve(("district", "dlnd01", "desg:district judge-04")) is not None
    # miss
    assert p.resolve(make_key("district", "DLND01", "999")) is None


def test_malformed_row_is_skipped_not_raised():
    p = CuratedMapProvider.from_rows([{"garbage": 1}, ROWS[0]])
    assert p.resolve(make_key("district", "DLND01", "75")) is not None


def test_designation_only_row_resolves_via_normalised_key():
    """A row with NO court_no but a designation resolves via desg: alias.

    This exercises the activation fix: the directory is keyed by designation
    (no court_no), so the alias must be queryable via normalize_designation().
    """
    rows = [{
        "scope": "district",
        "complex": "DLND0100",
        "designation": "District Judge-01",
        "vendor": "webex_personal_room",
        "url": "https://districtcourtdelhi.webex.com/meet/x",
    }]
    p = CuratedMapProvider.from_rows(rows)
    # court_no-keyed lookup must miss (no court_no in the row)
    assert p.resolve(make_key("district", "DLND0100", "75")) is None
    # desg-keyed lookup must hit, including suffix-space variants
    nd = normalize_designation("district judge-01")
    assert p.resolve(("district", "dlnd0100", "desg:" + nd)) is not None
    nd_variant = normalize_designation("District Judge- 01")
    assert p.resolve(("district", "dlnd0100", "desg:" + nd_variant)) is not None


def test_compound_cum_designation_resolves_by_each_role():
    """A "X cum Y" officer may be listed under just one role, so the row must
    resolve via each cum-separated part, not only the full compound string."""
    rows = [{
        "scope": "district", "complex": "HRRH02",
        "designation": "CJ(JD) cum JMIC",
        "vendor": "google_meet", "url": "https://meet.google.com/abc-defg-hij",
    }]
    p = CuratedMapProvider.from_rows(rows)
    # eCourts lists this case as just "Civil Judge (Junior Division)".
    k = ("district", "hrrh02", "desg:" + normalize_designation("Civil Judge (Junior Division)"))
    assert p.resolve(k) is not None
    # the full compound key still resolves too.
    kf = ("district", "hrrh02", "desg:" + normalize_designation("CJ(JD) cum JMIC"))
    assert p.resolve(kf) is not None


def test_full_designation_wins_over_another_rows_cum_part():
    """A specific full-designation row is not overwritten by another row's cum-part."""
    rows = [
        {"scope": "district", "complex": "HRRH02", "designation": "Civil Judge (Junior Division)",
         "vendor": "google_meet", "url": "https://meet.google.com/FULL"},
        {"scope": "district", "complex": "HRRH02", "designation": "CJ(JD) cum JMIC",
         "vendor": "google_meet", "url": "https://meet.google.com/PART"},
    ]
    p = CuratedMapProvider.from_rows(rows)
    a = p.resolve(("district", "hrrh02", "desg:" + normalize_designation("Civil Judge (Junior Division)")))
    assert a is not None and a.url.endswith("/FULL")
