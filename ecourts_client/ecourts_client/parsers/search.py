"""Parsers for search-mode endpoints.

Both `showDataWebService.php` (party search) and `caseNumberSearch.php` return the same
shape: a dict with numeric-string keys (`"0"`, `"1"`, ...) for each establishment, plus
`no_of_establishments`. Each establishment value is `{court_code, establishment_name, caseNos: [...]}`.

We flatten across establishments into a single list of CaseStubs.
"""
from __future__ import annotations

from typing import Any

from ecourts_client.errors import SchemaChanged
from ecourts_client.models import CaseStub


def _flatten_search_response(response: dict[str, Any]) -> list[CaseStub]:
    """Walk the numeric-keyed establishment buckets and return a flat list of CaseStubs."""
    if not isinstance(response, dict):
        raise SchemaChanged(field="search.response", reason=f"expected dict, got {type(response).__name__}")

    out: list[CaseStub] = []
    for key, bucket in response.items():
        if not key.isdigit() or not isinstance(bucket, dict):
            continue
        court = (bucket.get("establishment_name") or "").strip() or "(unknown court)"
        for row in bucket.get("caseNos") or []:
            cino = (row.get("cino") or "").strip()
            if not cino:
                continue
            pet = (row.get("pet_name") or "").strip()
            res = (row.get("res_name") or "").strip()
            title = f"{pet} vs {res}" if pet and res else (pet or res or "(unknown title)")
            year = row.get("reg_year") or row.get("case_year")
            try:
                filing_year: int | None = int(year) if year else None
            except (TypeError, ValueError):
                filing_year = None
            out.append(
                CaseStub(
                    cnr=cino,
                    title=title,
                    case_number=str(row.get("case_no") or ""),
                    court=court,
                    filing_year=filing_year,
                    stage=None,  # search response doesn't carry stage; need fetch_case for that
                )
            )
    return out


def parse_party_search(response: dict[str, Any]) -> list[CaseStub]:
    return _flatten_search_response(response)


def parse_case_number_search(response: dict[str, Any]) -> list[CaseStub]:
    return _flatten_search_response(response)
