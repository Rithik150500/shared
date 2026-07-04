"""Regression: the HIGH COURT case-types lister must send the SINGULAR
``court_code`` -- the exact REVERSE of the District Court lister.

eCourts v4 diverges by tier on ``caseTypesWebService.php``:

    District Court : court_code_arr required; singular court_code -> error_ERROR_courtcode4
    High Court     : singular court_code required; court_code_arr -> "error"

Both were live-confirmed 2026-07-05 (HC against High Court of Rajasthan(9),
dist/court 1/1: singular -> 117 case types; array -> ECourtsError "error").

This test exists to stop a well-meaning future "parity" change that flips the HC
lister to ``court_code_arr`` to match the District fix -- that would break the HC
add-case flow (empty case-type list -> user can't pick the right type). See
``test_district_lister_courtcode.py`` for the mirror-image DC assertion and
docs/RE_NOTES_v4.md for the disasm/live evidence.
"""
from __future__ import annotations

from ecourts_client.highcourt import HighCourtClient


class _RecordingSession:
    """Records the cleartext payload each endpoint is called with and returns a
    canned response (stands in for the encrypted-transport Session)."""

    def __init__(self, response: dict) -> None:
        self._response = response
        self.calls: list[tuple[str, dict]] = []

    def call(self, endpoint: str, payload: dict) -> dict:
        self.calls.append((endpoint, dict(payload)))
        return self._response


def _client_with(response: dict) -> HighCourtClient:
    client = HighCourtClient()
    client._session = _RecordingSession(response)  # type: ignore[assignment]
    return client


def test_hc_list_case_types_sends_singular_court_code_not_array():
    resp = {"case_types": [{"case_type": "11~CMA - CIVIL MISCELLANEOUS APPEAL#37~CRLMA - CRIMINAL MISCELLANEOUS APPLICATION"}]}
    client = _client_with(resp)

    types = client.list_case_types(state_code="9", district_code="1", court_code="1")

    endpoint, payload = client._session.calls[-1]  # type: ignore[attr-defined]
    assert endpoint == "caseTypesWebService.php"
    assert payload.get("court_code") == "1", payload
    assert "court_code_arr" not in payload, (
        "HC caseTypesWebService.php requires the SINGULAR court_code; the "
        f"court_code_arr form returns 'error' (opposite of DC). payload was {payload}"
    )
    assert [t.code for t in types] == ["11", "37"]
    # The abbreviation is preserved in the parsed name so the bot picker can
    # resolve an exact-abbrev match ("CMA" -> code 11).
    assert types[0].name == "CMA - CIVIL MISCELLANEOUS APPEAL"
