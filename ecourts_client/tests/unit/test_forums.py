"""Phase-1C adapter framework: Forum taxonomy, registry, capabilities, routing.

Pure/no-network — importing ecourts_client runs apply_sync_resilience() and
registers the eCourts adapters; everything here exercises the forum-first layer
without touching the transport.
"""
from __future__ import annotations

import pytest

from ecourts_client import (
    DistrictCourtClient,
    Forum,
    ForumAdapter,
    ForumNotAutomated,
    HighCourtClient,
    IdentifierMalformed,
    get_adapter,
    has_automated_adapter,
)
from ecourts_client.errors import CNRMalformed
from ecourts_client.forums import ECOURTS_FORUMS, IdentifierKind
from ecourts_client.routing import forum_for_cnr, validate_identifier

NON_ECOURTS = [Forum.SUPREME_COURT, Forum.CONSUMER, Forum.DRT, Forum.ARBITRATION]


def test_forum_values_match_db_discriminator():
    # Forum.value MUST equal the cases.forum DB column strings (single namespace).
    assert Forum.ECOURTS_DISTRICT.value == "ecourts_district"
    assert Forum.ECOURTS_HIGHCOURT.value == "ecourts_highcourt"
    assert {f.value for f in Forum} == {
        "ecourts_district", "ecourts_highcourt", "supreme_court",
        "consumer", "drt", "arbitration",
    }


def test_ecourts_forums_set():
    assert ECOURTS_FORUMS == {Forum.ECOURTS_DISTRICT, Forum.ECOURTS_HIGHCOURT}


def test_ecourts_adapters_registered():
    assert has_automated_adapter(Forum.ECOURTS_DISTRICT)
    assert has_automated_adapter(Forum.ECOURTS_HIGHCOURT)
    assert isinstance(get_adapter(Forum.ECOURTS_DISTRICT), DistrictCourtClient)
    assert isinstance(get_adapter(Forum.ECOURTS_HIGHCOURT), HighCourtClient)


@pytest.mark.parametrize("forum", NON_ECOURTS)
def test_non_ecourts_forums_not_automated(forum):
    assert not has_automated_adapter(forum)
    with pytest.raises(ForumNotAutomated):
        get_adapter(forum)


def test_adapters_satisfy_protocol_and_capabilities():
    dc = DistrictCourtClient()
    assert isinstance(dc, ForumAdapter)  # runtime_checkable structural check
    assert dc.capabilities.forum is Forum.ECOURTS_DISTRICT
    assert dc.capabilities.identifier_kind is IdentifierKind.CNR
    assert dc.capabilities.supports_fetch is True
    assert dc.capabilities.is_manual is False

    hc = HighCourtClient()
    assert isinstance(hc, ForumAdapter)
    assert hc.capabilities.forum is Forum.ECOURTS_HIGHCOURT


def test_forum_for_cnr():
    assert forum_for_cnr("MHCC010054732024") is Forum.ECOURTS_DISTRICT
    assert forum_for_cnr("DLHC010012342024") is Forum.ECOURTS_HIGHCOURT


def test_validate_identifier_cnr_forum():
    validate_identifier(Forum.ECOURTS_DISTRICT, "MHCC010054732024")  # no raise
    with pytest.raises(CNRMalformed):
        validate_identifier(Forum.ECOURTS_DISTRICT, "not-a-cnr")


def test_validate_identifier_manual_is_noop():
    # Arbitration is manual → any opaque ref (incl. empty) is accepted here.
    validate_identifier(Forum.ARBITRATION, "In re: ACME / Beta, Sole Arbitrator")
    validate_identifier(Forum.ARBITRATION, "")


@pytest.mark.parametrize("forum", [Forum.SUPREME_COURT, Forum.CONSUMER, Forum.DRT])
def test_validate_identifier_non_ecourts_nonempty(forum):
    validate_identifier(forum, "SLP(C) 12345/2026")  # no raise
    with pytest.raises(IdentifierMalformed):
        validate_identifier(forum, "   ")
