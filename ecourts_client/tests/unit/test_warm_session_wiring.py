from __future__ import annotations

from ecourts_client._session import get_warm_session, reset_warm_sessions


def test_district_client_uses_warm_session():
    reset_warm_sessions()
    from ecourts_client.district import DistrictCourtClient
    a, b = DistrictCourtClient(), DistrictCourtClient()
    assert a._session is b._session is get_warm_session("district")


def test_highcourt_client_uses_warm_session():
    reset_warm_sessions()
    from ecourts_client.highcourt import HighCourtClient
    a, b = HighCourtClient(), HighCourtClient()
    assert a._session is b._session is get_warm_session("highcourt")


def test_dc_and_hc_clients_do_not_share_a_session():
    reset_warm_sessions()
    from ecourts_client.district import DistrictCourtClient
    from ecourts_client.highcourt import HighCourtClient
    assert DistrictCourtClient()._session is not HighCourtClient()._session
