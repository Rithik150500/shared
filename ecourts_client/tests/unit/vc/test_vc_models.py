from ecourts_client.vc.models import VCAccess, VCVendor, VCLinkType, make_key, normalize_designation


def test_vcaccess_defaults_and_key_normalises():
    a = VCAccess(vendor=VCVendor.WEBEX_PERSONAL_ROOM, link_type=VCLinkType.JOIN_URL,
                 url="https://districtcourtdelhi.webex.com/meet/x")
    assert a.meeting_id is None and a.passcode is None
    assert a.requires_intimation is False and a.persistent is True
    assert make_key("district", "DLND01", "75") == ("district", "dlnd01", "75")


def test_normalize_designation_suffix_variants():
    """All common suffix-spacing forms normalise to the same canonical string."""
    canonical = "district judge-01"
    assert normalize_designation("District Judge-01") == canonical
    assert normalize_designation("District Judge- 01") == canonical
    assert normalize_designation("District Judge - 01") == canonical
    assert normalize_designation("district judge-01") == canonical
    assert normalize_designation("  District  Judge - 01  ") == canonical


def test_normalize_designation_none_and_empty():
    assert normalize_designation(None) == ""
    assert normalize_designation("") == ""


def test_normalize_designation_no_suffix():
    """Designations without a trailing -NN are still lowercased/trimmed."""
    assert normalize_designation("Chief Justice") == "chief justice"
    assert normalize_designation("  Principal District Judge  ") == "principal district judge"
