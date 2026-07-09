"""labour_court + industrial_tribunal are first-class MANUAL tribunal kinds."""
from ecourts_client import Forum, TribunalKind
from ecourts_client.client import has_automated_adapter


def test_labour_kinds_exist_with_expected_values():
    assert TribunalKind.LABOUR_COURT.value == "labour_court"
    assert TribunalKind.INDUSTRIAL_TRIBUNAL.value == "industrial_tribunal"


def test_labour_kinds_are_manual():
    # No adapter registered → manual (never auto-refreshed).
    assert has_automated_adapter(Forum.TRIBUNAL, kind=TribunalKind.LABOUR_COURT) is False
    assert has_automated_adapter(Forum.TRIBUNAL, kind=TribunalKind.INDUSTRIAL_TRIBUNAL) is False
