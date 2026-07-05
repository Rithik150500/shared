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

# Consumer became automated in Phase 2 (the e-Jagriti adapter); the rest are
# still manual-only until their own phases.
UNAUTOMATED_FORUMS = [Forum.DRT, Forum.ARBITRATION]


def test_forum_values_match_db_discriminator():
    # Forum.value MUST equal the cases.forum DB column strings (single namespace).
    assert Forum.ECOURTS_DISTRICT.value == "ecourts_district"
    assert Forum.ECOURTS_HIGHCOURT.value == "ecourts_highcourt"
    assert {f.value for f in Forum} == {
        "ecourts_district", "ecourts_highcourt", "supreme_court",
        "consumer", "drt", "arbitration", "tribunal",
    }


def test_tribunal_registry_is_kind_keyed_and_backward_compatible():
    from ecourts_client import TribunalKind, has_automated_adapter
    # Forum-only calls are unchanged (kind defaults to None).
    assert has_automated_adapter(Forum.CONSUMER) is True
    assert has_automated_adapter(Forum.TRIBUNAL) is False
    # NCLAT is the first automated kind (T3 Wave-0); every OTHER kind stays manual.
    assert has_automated_adapter(Forum.TRIBUNAL, kind=TribunalKind.NCLAT) is True
    assert all(
        not has_automated_adapter(Forum.TRIBUNAL, kind=k)
        for k in TribunalKind
        if k is not TribunalKind.NCLAT
    )
    # DRT/DRAT live as tribunal kinds; the family is non-empty and includes them.
    assert {"drt", "drat", "nclt", "nclat", "cat", "itat"} <= {k.value for k in TribunalKind}


def test_ecourts_forums_set():
    assert ECOURTS_FORUMS == {Forum.ECOURTS_DISTRICT, Forum.ECOURTS_HIGHCOURT}


def test_ecourts_adapters_registered():
    assert has_automated_adapter(Forum.ECOURTS_DISTRICT)
    assert has_automated_adapter(Forum.ECOURTS_HIGHCOURT)
    assert isinstance(get_adapter(Forum.ECOURTS_DISTRICT), DistrictCourtClient)
    assert isinstance(get_adapter(Forum.ECOURTS_HIGHCOURT), HighCourtClient)


@pytest.mark.parametrize("forum", UNAUTOMATED_FORUMS)
def test_unautomated_forums_have_no_adapter(forum):
    assert not has_automated_adapter(forum)
    with pytest.raises(ForumNotAutomated):
        get_adapter(forum)


def test_consumer_adapter_registered():
    # Phase 2: the e-Jagriti adapter makes the Consumer forum automated.
    from ecourts_client.consumer import ConsumerClient
    assert has_automated_adapter(Forum.CONSUMER)
    assert isinstance(get_adapter(Forum.CONSUMER), ConsumerClient)


def test_supreme_adapter_registered():
    # Phase 3: the com.nic.sciapp adapter makes the Supreme Court forum automated.
    from ecourts_client.supreme import SupremeCourtClient
    assert has_automated_adapter(Forum.SUPREME_COURT)
    assert isinstance(get_adapter(Forum.SUPREME_COURT), SupremeCourtClient)


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
