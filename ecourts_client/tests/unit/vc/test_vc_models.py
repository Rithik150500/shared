from ecourts_client.vc.models import VCAccess, VCVendor, VCLinkType, make_key


def test_vcaccess_defaults_and_key_normalises():
    a = VCAccess(vendor=VCVendor.WEBEX_PERSONAL_ROOM, link_type=VCLinkType.JOIN_URL,
                 url="https://districtcourtdelhi.webex.com/meet/x")
    assert a.meeting_id is None and a.passcode is None
    assert a.requires_intimation is False and a.persistent is True
    assert make_key("district", "DLND01", "75") == ("district", "dlnd01", "75")
