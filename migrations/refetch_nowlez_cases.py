"""Re-fetch all Nowlez cases from eCourts via shared client -> new unified schema.

Idempotent. Safe to re-run. Reads from the temp ``_legacy_nowlez_client_cases``
table that the sub-project D cutover left behind for sub-project A.

Sub-project A PR 6 A.4 — placeholder cleanup
--------------------------------------------
During the PR 5.3 bake period, the Nowlez backend
(``backend/preprocessing.py``) best-effort-writes each new case to
Postgres alongside SQLite. When the phone bridge fails to resolve a real
``users.id`` it falls back to ``uuid.uuid5(_PG_USER_FALLBACK_NS,
client_id)`` (see ``backend/helpers.py::_resolve_pg_user_id`` at commit
``fef6e60`` on branch ``feat/subproject-a-pr6-cutover``). Those
placeholder rows must not survive the PR 6 cutover.

The refetch is the right cleanup hook because (a) it touches every
legacy case exactly once and (b) it has the real ``users.id`` already
(D's cutover put it in ``_legacy_nowlez_client_cases.user_id``). Before
each upsert we delete any pre-existing ``cases`` row keyed on
``(uuid5(client_id), cnr)``. Idempotent: re-running after a successful
cleanup is a no-op (DELETE matching nothing).

The ``_PG_USER_FALLBACK_NS`` constant below MUST match the Nowlez
backend byte-for-byte — drift would mean placeholders written during the
bake do not get matched here. A test (``test_refetch_with_bridge.py
::test_placeholder_namespace_matches_backend``) asserts this.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

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


# Must match backend/helpers.py::_PG_USER_FALLBACK_NS in the Nowlez repo
# (commit fef6e60 on branch feat/subproject-a-pr6-cutover). Used to
# reconstruct the uuid5 placeholder PR 5.3 would have written for a given
# SQLite client_id, so we can delete the placeholder row before upserting
# the canonical (real_user_id, cnr) row.
_PG_USER_FALLBACK_NS = uuid.UUID("5fa9c8c8-3a17-4b9e-9c4e-0c2cf5e1d9aa")


_LEGACY_QUERY = sa.text(
    "SELECT id, user_id, cnr, client_id, refresh_enabled, notes "
    "FROM _legacy_nowlez_client_cases ORDER BY id"
)


def _cleanup_placeholder_row(session, *, client_id: str | None, cnr: str) -> None:
    """Delete any PR 5.3-era placeholder row at ``(uuid5(client_id), cnr)``.

    The Nowlez backend's bake-period dual-write fell back to a
    deterministic ``uuid5`` when the phone bridge couldn't resolve a real
    ``users.id``. We recompute that same uuid5 here and remove the row if
    present, so the canonical refetch upsert (under the real ``user_id``
    from ``_legacy_nowlez_client_cases``) doesn't leave a duplicate
    behind.

    Skips when ``client_id`` is NULL — those rows could not have produced
    a uuid5 placeholder in the first place (no client_id → no namespaced
    hash input). Cleanup is also a no-op for SQLite-only test sessions
    that don't have a stale row to begin with; ``case_dao.delete_case``
    returns False rather than raising.

    Logs at INFO when a cleanup actually happened so operators can audit
    bake-period placeholder accrual after the run.
    """
    if not client_id:
        return
    placeholder_uid = uuid.uuid5(_PG_USER_FALLBACK_NS, client_id)
    deleted = case_dao.delete_case(session, user_id=placeholder_uid, cnr=cnr)
    if deleted:
        logger.info(
            "cleaned up bake placeholder row: client_id=%s cnr=%s "
            "placeholder_uid=%s",
            client_id, cnr, placeholder_uid,
        )


async def migrate_one_case(row: dict) -> str:
    user_id = row["user_id"]
    cnr = row["cnr"]
    with get_session() as s:
        if case_dao.exists(s, user_id=user_id, cnr=cnr):
            # Even on the skip-fast path, attempt the placeholder cleanup
            # so a partial prior run (canonical written, placeholder
            # delete failed) eventually self-heals on re-run. No-op if
            # the placeholder is already gone.
            _cleanup_placeholder_row(s, client_id=row.get("client_id"), cnr=cnr)
            s.commit()
            return "skipped"

    try:
        fresh = await fetch_case(cnr)
    except CNRMalformed:
        return "malformed"
    except CNRNotFound:
        with get_session() as s:
            _cleanup_placeholder_row(s, client_id=row.get("client_id"), cnr=cnr)
            case_dao.mark_cnr_not_found(s, user_id=user_id, cnr=cnr)
            s.commit()
        return "cnr_not_found"
    except (CourtSiteDown, CircuitOpen):
        return "retry_later"
    except Exception:
        logger.exception("unexpected error fetching %s", cnr)
        return "error"

    with get_session() as s:
        # PR 6 A.4: remove any uuid5 placeholder row PR 5.3's bake-period
        # dual-write may have left behind, BEFORE the canonical upsert under
        # the real user_id. Same session/transaction as the upsert so the
        # two are atomic. See module docstring for full rationale.
        _cleanup_placeholder_row(s, client_id=row.get("client_id"), cnr=cnr)

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
