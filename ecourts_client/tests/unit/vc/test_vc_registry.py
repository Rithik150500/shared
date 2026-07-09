from ecourts_client.vc.models import VCAccess, VCVendor, VCLinkType, make_key
from ecourts_client.vc.registry import register_vc_provider, get_vc_provider, resolve_vc


class _Stub:
    def resolve(self, key):
        return VCAccess(VCVendor.JITSI, VCLinkType.JOIN_URL, "https://meet.jit.si/x") \
            if key[1] == "hit" else None


def test_registry_and_first_non_none():
    register_vc_provider("stub", _Stub())
    assert get_vc_provider("stub") is not None
    assert resolve_vc(make_key("district", "hit", "1"), providers=[_Stub()]) is not None
    assert resolve_vc(make_key("district", "miss", "1"), providers=[_Stub()]) is None
