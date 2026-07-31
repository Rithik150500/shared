"""``I.A. N </br>IN CS(COMM) -M//YYYY`` rows must yield the MAIN case, not the application.

A Delhi HC cause-list row for an interlocutory application prints two case
numbers: the application, then an ``</br>IN`` marker, then the suit it sits in.
The parser took the first ``number/year`` it saw, so the row resolved to the
I.A. -- which nobody tracks -- and the suit, which the advocate does track,
never appeared in ``cause_list_rows``.

This is how RSRL's ``CS(COMM) 1309/2025`` went missing from the 2026-07-31
digest: item 38 on bench 11865 (Justice Jyoti Singh) parsed as
``I.A./30734/2025``. 18 of that bench's 67 rows are this shape, so the defect
costs roughly a quarter of a commercial bench's listings.

Column texts below are verbatim captures from the live 2026-07-31 PDF, taken by
instrumenting ``_rebuild_case_number_from_column``. Note the marker arrives in
three manglings -- ``</br>IN``, ``< /br>IN``, ``</br >IN`` -- because the
position-based reflow splits the literal ``<br>`` tag at column boundaries.
"""
from __future__ import annotations

import pytest

from ecourts_client.parsers.cause_list_hc_pdf import _main_case_from_column_text


class TestMainCaseIsPreferred:
    @pytest.mark.parametrize(
        "col_text,expected",
        [
            # The customer's row.
            ("I.A. 30734/2025 </br>IN CS(COMM) -1309//2025", "CS(COMM)/1309/2025"),
            # Several applications before the marker.
            (
                "I.A. 15258/2022 ,I.A. 11470/2017 ,I.A. 10663/2017 </br>IN CS(COMM) -611//2017",
                "CS(COMM)/611/2017",
            ),
            ("I.A. 13531/2026 ,I.A. 13424/2026 </br>IN CS(COMM) -373//2024", "CS(COMM)/373/2024"),
            ("I.A. 41729/2024 </br>IN CS(COMM) -883//2024", "CS(COMM)/883/2024"),
            # A non-I.A. application type.
            ("CRL.M.A. 12694/2026 </br>IN CS(COMM) -416//2025", "CS(COMM)/416/2025"),
            # Marker mangled as '< /br>IN'.
            ("I.A. 2779/2026< /br>IN CS(COMM) -1262//2025", "CS(COMM)/1262/2025"),
            ("I.A. 4098/2026,I .A. 6547/2026< /br>IN CS(COMM) -141//2026", "CS(COMM)/141/2026"),
            # Marker mangled as '</br >IN', with a CCP(O) in the application list.
            (
                "I.A. 9693/2026,I .A. 9696/2026, CCP(O) 53/2026</br >IN CS(COMM) -377//2026",
                "CS(COMM)/377/2026",
            ),
            # Application list with a reflow-split 'I .A.'.
            (
                "I.A. 4089/2025,I .A. 16783/2025 ,I.A. 20248/2026 </br>IN CS(COMM) -132//2025",
                "CS(COMM)/132/2025",
            ),
            # An application with no number at all before the marker.
            ("I.A. ,I.A. 12419/2025 </br>IN CS(COMM) -46//2024", "CS(COMM)/46/2024"),
        ],
    )
    def test_returns_the_case_after_the_marker(self, col_text, expected):
        assert _main_case_from_column_text(col_text) == expected


class TestNoMarker:
    @pytest.mark.parametrize(
        "col_text",
        [
            "CS(COMM) -451//2026",
            "W.P.(C)- 8287//2026",
            "C.A.(COMM .IPD-PAT)- 430//2022",
            "",
            "no case number here",
        ],
    )
    def test_returns_none_so_caller_keeps_existing_behaviour(self, col_text):
        """Without a marker this helper must abstain -- the existing
        first-match logic is correct for ordinary rows."""
        assert _main_case_from_column_text(col_text) is None

    def test_bare_word_in_is_not_a_marker(self):
        """Only the <br>-prefixed form is the structural marker. A stray 'IN'
        (e.g. inside a party or act name that leaked into the column) must not
        trigger a rewrite."""
        assert _main_case_from_column_text("SUIT IN REM 12/2026") is None


class TestEndToEndThroughTheParser:
    def test_hyphenated_main_case_survives(self):
        """An IPD main case keeps its hyphen, which the CNR resolver now
        accepts and the case-type cache now keys on."""
        got = _main_case_from_column_text(
            "I.A. 19063/2026 </br>IN C.A.(COMM.IPD-TM) -49//2026"
        )
        assert got == "C.A.(COMM.IPD-TM)/49/2026"

    def test_result_is_parseable_by_the_cnr_resolver_regex(self):
        """The whole point of the rewrite: the output must feed back-resolution."""
        import re

        # Same shape as bot.causelist.cnr_resolver._CASE_NUMBER_RE.
        pat = re.compile(r"^\s*([A-Z][A-Z()\.\s\-]*?)\s*/\s*(\d+)\s*/\s*(\d{4})\s*$", re.I)
        for col in (
            "I.A. 30734/2025 </br>IN CS(COMM) -1309//2025",
            "I.A. 19063/2026 </br>IN C.A.(COMM.IPD-TM) -49//2026",
        ):
            out = _main_case_from_column_text(col)
            assert out is not None
            assert pat.match(out), out
