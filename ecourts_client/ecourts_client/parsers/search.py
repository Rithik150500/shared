"""Parsers for search-mode endpoints.

`showDataWebService.php` (party search) and `caseNumberSearch.php` return one
of two shapes depending on the court tier:

* **District Court** -- a dict with numeric-string keys (`"0"`, `"1"`, ...),
  one per establishment, plus `no_of_establishments`. The DC app
  (main.js:displayCasesTable) sends the whole establishment CSV in
  `court_code_arr` and the server returns all buckets at once.
* **High Court** -- a FLAT single-establishment object
  `{court_code, establishment_name, caseNos: [...]}` at the top level (no
  numeric keys, no `no_of_establishments`). The HC app
  (main_hc.js:displayCasesTable) loops one call per bench and reads
  `data.caseNos` directly. Verified live against Bombay HC.

Each establishment value is `{court_code, establishment_name, caseNos: [...]}`.
We flatten across establishments into a single list of CaseStubs, handling
both shapes.
"""
from __future__ import annotations

from typing import Any

from ecourts_client.errors import SchemaChanged
from ecourts_client.models import CaseStub


def _establishment_buckets(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the establishment buckets regardless of DC/HC response shape.

    HC returns a single establishment flat at the top level (detected by a
    top-level ``caseNos`` key); DC returns numeric-keyed sub-dicts.
    """
    if "caseNos" in response:
        return [response]
    return [v for k, v in response.items() if k.isdigit() and isinstance(v, dict)]


def _flatten_search_response(response: dict[str, Any]) -> list[CaseStub]:
    """Walk the establishment buckets (DC or HC shape) and return a flat list of CaseStubs."""
    if not isinstance(response, dict):
        raise SchemaChanged(field="search.response", reason=f"expected dict, got {type(response).__name__}")

    out: list[CaseStub] = []
    for bucket in _establishment_buckets(response):
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
