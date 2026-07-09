from ecourts_client.vc.models import (
    VCAccess, VCVendor, VCLinkType, make_key, normalize_designation,
)
from ecourts_client.vc.curated import CuratedMapProvider


def test_vcaccess_room_in_to_meta():
    a = VCAccess(VCVendor.WEBEX_PERSONAL_ROOM, VCLinkType.JOIN_URL,
                 "https://x.webex.com/meet/y", room="611")
    assert a.room == "611"
    assert a.to_meta()["room"] == "611"


def test_vcaccess_room_defaults_none():
    a = VCAccess(VCVendor.WEBEX_PERSONAL_ROOM, VCLinkType.JOIN_URL,
                 "https://x.webex.com/meet/y")
    assert a.room is None
    assert a.to_meta()["room"] is None


def test_curated_row_carries_room():
    p = CuratedMapProvider.from_rows([{
        "scope": "district", "complex": "DLSW0100", "designation": "PDSJ",
        "room": "611", "vendor": "webex_personal_room",
        "url": "https://districtcourtdwarka.webex.com/meet/PDSJRoom611",
    }])
    acc = p.resolve(("district", "dlsw0100", "desg:" + normalize_designation("PDSJ")))
    assert acc is not None
    assert acc.room == "611"
    assert acc.to_meta()["room"] == "611"
    # also reachable by court_no key when present; here only designation -> room carried
    assert p.resolve(make_key("district", "DLSW0100", "nope")) is None
