"""Regression: eCourts mobile API v4 DC dropdown listers must send the
``court_code_arr`` (array/CSV) param, NOT the singular ``court_code``.

v4 rejects the singular form on the two *dropdown* listers with
``error_ERROR_courtcode4`` (the singular ``court_code`` survives only on the
single-court endpoints -- cause-list / daily-business). Live-confirmed
2026-07-05 against Maharashtra(1)/Jalgaon(3)/Jamner (est 14):

    court_code       -> ECourtsError: caseTypesWebService.php: error_ERROR_courtcode4
    court_code_arr   -> OK (~100 case types)   [and identically for police stations]

The app disasm (``~/ecourts_re/disasm.hasm``, fetchCaseTypes payload) builds
``{state_code, dist_code, court_code_arr, bilingual_flag, language_flag}``.
See docs/RE_NOTES_v4.md.
"""
from __future__ import annotations

from ecourts_client.district import DistrictCourtClient


class _RecordingSession:
    """Stands in for the encrypted-transport Session: records the cleartext
    payload each endpoint is called with and returns a canned response."""

    def __init__(self, response: dict) -> None:
        self._response = response
        self.calls: list[tuple[str, dict]] = []

    def call(self, endpoint: str, payload: dict) -> dict:
        self.calls.append((endpoint, dict(payload)))
        return self._response


def _client_with(response: dict) -> DistrictCourtClient:
    client = DistrictCourtClient()
    client._session = _RecordingSession(response)  # type: ignore[assignment]
    return client


def test_list_case_types_sends_court_code_arr_not_singular_court_code():
    resp = {"case_types": [{"case_type": "46~AC Cri.M.A.#84~CC NI ACT"}]}
    client = _client_with(resp)

    types = client.list_case_types(state_code="1", district_code="3", court_code="14")

    endpoint, payload = client._session.calls[-1]  # type: ignore[attr-defined]
    assert endpoint == "caseTypesWebService.php"
    assert payload.get("court_code_arr") == "14", payload
    assert "court_code" not in payload, (
        f"v4 rejects singular court_code on caseTypesWebService.php "
        f"(error_ERROR_courtcode4); payload was {payload}"
    )
    assert [t.code for t in types] == ["46", "84"]


def test_list_police_stations_sends_court_code_arr_not_singular_court_code():
    resp = {"police_stationlist": [{"police_station": "1~PS One#2~PS Two", "uniform_code": {}}]}
    client = _client_with(resp)

    stations = client.list_police_stations(state_code="1", district_code="3", court_code="14")

    endpoint, payload = client._session.calls[-1]  # type: ignore[attr-defined]
    assert endpoint == "policeStationWebService.php"
    assert payload.get("court_code_arr") == "14", payload
    assert "court_code" not in payload, (
        f"v4 rejects singular court_code on policeStationWebService.php "
        f"(error_ERROR_courtcode4); payload was {payload}"
    )
    assert [s.code for s in stations] == ["1", "2"]
