"""LIVE canary: the hardcoded NCDRC commission id still resolves on e-Jagriti.

Why this exists
---------------
``NCDRC_COMMISSION_ID = 11000000`` is an **undocumented constant on an unofficial
NIC API**. It is not discoverable from any lister — e-Jagriti's own SPA hardcodes
it the same way — so nothing in the normal test suite can detect NIC renumbering
it. If that happens, NCDRC lookups fail closed (empty list / CNRNotFound) and
users just see "no case found"; nothing errors loudly. This is the tripwire, and
it is exactly the "alert on a sustained shift" guardrail that
docs/spike-ejagriti-transport.md §7 prescribes.

Network-gated: skipped unless ``ECOURTS_LIVE_TESTS=1``, so CI and local runs stay
hermetic. Run deliberately:

    ECOURTS_LIVE_TESTS=1 pytest tests/integration/test_consumer_ncdrc_live.py -v

A failure here is a signal to investigate, NOT necessarily a code bug — the
portal may simply be down. Re-run before concluding the constant has moved.
"""
from __future__ import annotations

import os
from datetime import date

import pytest

from ecourts_client.consumer import NCDRC_COMMISSION_ID, ConsumerClient

pytestmark = pytest.mark.skipif(
    os.environ.get("ECOURTS_LIVE_TESTS") != "1",
    reason="live network test; set ECOURTS_LIVE_TESTS=1 to run",
)


def test_ncdrc_id_still_returns_real_cases():
    """The id must still resolve NCDRC rows via the name-search path.

    Name search (not case-number) because it needs no known-good case number to
    stay valid as cases age out.

    The window is a FIXED, CLOSED historical range, not a rolling one: those
    filings are settled so the result stays stable, and — measured live
    2026-07-30 — a broad term over this 2-year window answers in ~2.7s where the
    same term over 2015→today takes ~44s and blows the 30s client timeout. The
    cost is driven by how many rows match server-side, not by the window alone.
    """
    stubs = ConsumerClient().search_by_name(
        commission_id=NCDRC_COMMISSION_ID,
        name="a",
        role="complainant",
        from_date=date(2020, 1, 1),
        to_date=date(2021, 12, 31),
        size=10,
    )
    assert stubs, (
        f"NCDRC commission id {NCDRC_COMMISSION_ID} returned NO rows. Either "
        "e-Jagriti is down or NIC renumbered the National Commission — re-derive "
        "it from the SPA bundle (grep the /static/js/main.*.js for 'NCDRC')."
    )
    # NCDRC normalises its case numbers to NC/<TYPE>/<n>/<year>. A drift here
    # means the id now points at some OTHER commission — worse than an outage,
    # because it would silently attach users to the wrong forum's cases.
    assert any(s.case_number.upper().startswith("NC/") for s in stubs), (
        f"id {NCDRC_COMMISSION_ID} resolved, but no row looks like an NCDRC "
        f"case number: {[s.case_number for s in stubs][:5]}"
    )


def test_ncdrc_is_offered_in_the_commission_cascade():
    """Regression guard for the original bug: NCDRC missing from the dropdown."""
    names = {c.commission_id: c.name for c in ConsumerClient().list_state_commissions()}
    assert NCDRC_COMMISSION_ID in names, "NCDRC absent from the commission cascade"
    # And it must still be absent upstream — if NIC ever adds it to the lister,
    # our injected entry becomes a DUPLICATE and should be removed.
    assert len([c for c in ConsumerClient().list_state_commissions()
                if c.commission_id == NCDRC_COMMISSION_ID]) == 1, (
        "NCDRC appears twice — NIC likely added it to "
        "getStateCommissionAndCircuitBench; drop the client-side injection."
    )
