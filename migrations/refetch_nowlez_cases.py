"""Re-fetch all Nowlez cases from eCourts via shared client -> new unified schema.

Idempotent. Safe to re-run. Reads from the temp ``_legacy_nowlez_client_cases``
table that the sub-project D cutover left behind for sub-project A.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import sqlalchemy as sa

from data_access import get_session
from data_access.daos import case_dao, order_dao
from ecourts_client import (
    CircuitOpen,
    CNRMalformed,
    CNRNotFound,
    CourtSiteDown,
    fetch_case,
)


logger = logging.getLogger(__name__)


_LEGACY_QUERY = sa.text(
    "SELECT id, user_id, cnr, client_id, refresh_enabled, notes "
    "FROM _legacy_nowlez_client_cases ORDER BY id"
)


async def migrate_one_case(row: dict) -> str:
    user_id = row["user_id"]
    cnr = row["cnr"]
    with get_session() as s:
        if case_dao.exists(s, user_id=user_id, cnr=cnr):
            return "skipped"

    try:
        fresh = await fetch_case(cnr)
    except CNRMalformed:
        return "malformed"
    except CNRNotFound:
        with get_session() as s:
            case_dao.mark_cnr_not_found(s, user_id=user_id, cnr=cnr)
            s.commit()
        return "cnr_not_found"
    except (CourtSiteDown, CircuitOpen):
        return "retry_later"
    except Exception:
        logger.exception("unexpected error fetching %s", cnr)
        return "error"

    with get_session() as s:
        new_case = case_dao.upsert_case(
            s,
            user_id=user_id,
            cnr=cnr,
            case_data=fresh,
            client_id=row.get("client_id"),
            refresh_enabled=bool(row.get("refresh_enabled", True)),
            notes=row.get("notes"),
        )
        for order_ref in fresh.orders:
            order_dao.ensure_nowlez_order(s, case_id=new_case.id, order_ref=order_ref)

        # Preserve PDF storage state from legacy table.
        legacy_orders = order_dao.get_legacy_orders_by_case(s, legacy_case_id=row["id"])
        if legacy_orders:
            existing = {
                o.order_id: o
                for o in order_dao.get_orders_for_case(s, case_id=new_case.id)
            }
            for legacy in legacy_orders:
                match = existing.get(legacy["order_id"])
                if match is None:
                    continue
                order_dao.upsert_nowlez_extension(
                    s,
                    order_id=match.id,
                    file_path=legacy.get("file_path"),
                    file_storage=(
                        "r2"
                        if (legacy.get("file_path") or "").startswith("orders/")
                        else "local"
                    ),
                    page_count=legacy.get("page_count"),
                    preprocessed=bool(legacy.get("preprocessed", False)),
                    preprocessed_at=legacy.get("preprocessed_at"),
                    retry_count=int(legacy.get("retry_count") or 0),
                    permanently_failed=bool(legacy.get("permanently_failed", False)),
                    uploaded_at=legacy.get("uploaded_at"),
                )
        s.commit()
    return "migrated"


async def main(concurrency: int = 8) -> dict[str, int]:
    with get_session() as s:
        rows = [dict(r) for r in s.execute(_LEGACY_QUERY).mappings().all()]

    logger.info("Migrating %d cases with concurrency=%d", len(rows), concurrency)

    sem = asyncio.Semaphore(concurrency)
    results: dict[str, int] = {}

    async def worker(row):
        async with sem:
            outcome = await migrate_one_case(row)
            results[outcome] = results.get(outcome, 0) + 1

    await asyncio.gather(*[worker(r) for r in rows])
    logger.info("Done: %s", results)
    return results


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.dry_run:
        with get_session() as s:
            count = s.execute(
                sa.text("SELECT COUNT(*) FROM _legacy_nowlez_client_cases")
            ).scalar()
        print(f"Would migrate {count} cases at concurrency={args.concurrency}")
        sys.exit(0)
    res = asyncio.run(main(concurrency=args.concurrency))
    sys.exit(0 if "error" not in res else 2)
