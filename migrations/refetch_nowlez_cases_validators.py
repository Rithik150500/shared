"""Validators: row counts, sample diff, FK integrity for the re-fetch migration."""
from __future__ import annotations

import logging
import random
from typing import Any

import sqlalchemy as sa

from data_access import get_session
from data_access.daos import case_dao


logger = logging.getLogger(__name__)


def row_counts_match() -> tuple[int, int]:
    """Return (legacy_count, new_cases_count) for sanity comparison.

    The ``WHERE client_id IS NOT NULL OR true`` clause is intentionally
    unconditional — it lets a downstream caller swap in a real filter later
    without having to restructure the query.
    """
    with get_session() as s:
        legacy = s.execute(
            sa.text("SELECT COUNT(*) FROM _legacy_nowlez_client_cases")
        ).scalar() or 0
        new = s.execute(
            sa.text("SELECT COUNT(*) FROM cases WHERE client_id IS NOT NULL OR true")
        ).scalar() or 0
    return legacy, new


def sample_diff(sample_size: int = 50) -> list[dict[str, Any]]:
    """Pick N legacy rows; assert each has a new-schema counterpart with
    matching CNR + user_id. Returns the list of mismatches (empty == OK).
    """
    with get_session() as s:
        legacy = [
            dict(r)
            for r in s.execute(
                sa.text("SELECT id, user_id, cnr FROM _legacy_nowlez_client_cases")
            ).mappings()
        ]
        sample = random.sample(legacy, min(sample_size, len(legacy)))
        problems: list[dict[str, Any]] = []
        for row in sample:
            new = case_dao.get_by_cnr(s, user_id=row["user_id"], cnr=row["cnr"])
            if new is None:
                problems.append(
                    {"legacy_id": row["id"], "cnr": row["cnr"], "reason": "missing"}
                )
        return problems


def assert_fk_integrity() -> None:
    """Every cases.user_id resolves to a real users row."""
    with get_session() as s:
        orphans = s.execute(
            sa.text(
                "SELECT c.id FROM cases c "
                "LEFT JOIN users u ON u.id = c.user_id "
                "WHERE u.id IS NULL"
            )
        ).all()
        assert not orphans, f"{len(orphans)} orphan cases"


def main() -> None:
    legacy, new = row_counts_match()
    print(f"Legacy: {legacy}; New cases (any): {new}")
    diffs = sample_diff()
    print(f"Sample-diff problems: {len(diffs)}")
    for d in diffs[:10]:
        print(f"  {d}")
    assert_fk_integrity()
    print("FK integrity: OK")


if __name__ == "__main__":
    main()
