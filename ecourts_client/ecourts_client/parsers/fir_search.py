"""Parser for the FIR-search endpoint (firNumberSearch.php).

Same response shape as party / case-number search: numeric-keyed buckets per
establishment with `caseNos` lists. We reuse the flatten logic from the search module.
"""
from __future__ import annotations

from typing import Any

from ecourts_client.models import CaseStub
from ecourts_client.parsers.search import _flatten_search_response


def parse_fir_search(response: dict[str, Any]) -> list[CaseStub]:
    return _flatten_search_response(response)
