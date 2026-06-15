"""Broadcast outcome / probe report for a campaign.

``summarize(session, campaign, *, tier=None) -> dict`` aggregates the
``wa_broadcast_log`` rows for a campaign (optionally filtered by tier) and
returns a dict with counts, percentages, and per-error-code breakdowns.

CLI usage::

    python -m whatsapp_delivery.tools.broadcast_report \\
        --campaign munshi_launch_2026_06 [--tier T1]

The CLI opens a real ``get_session()`` and prints the summary as a readable
table.  The ``summarize`` function is pure except for the DB read and is
independently unit-testable.

Spec reference: Phase 3 Task 7 (observability / ledger reporting).
"""
from __future__ import annotations

import argparse
import logging
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from data_access.models.broadcast import WaBroadcastLog

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known Meta error codes (for labelled reporting)
# ---------------------------------------------------------------------------

_CODE_UNDELIVERABLE = 131026   # recipient not on WhatsApp / undeliverable
_CODE_MARKETING_CAP = 131049   # marketing messaging cap exceeded (Meta-side)


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------


def summarize(
    session: Session,
    campaign: str,
    *,
    tier: Optional[str] = None,
) -> dict:
    """Aggregate ``wa_broadcast_log`` rows for ``campaign`` (optionally by ``tier``).

    Args:
        session: SQLAlchemy session.
        campaign: campaign identifier (matches ``WaBroadcastLog.campaign``).
        tier: optional tier prefix — when provided, only rows whose ``tier``
              equals this value are counted.

    Returns:
        dict with keys:

        - ``attempted``       — rows that left 'pending' (sent|delivered|read|failed)
        - ``sent``            — rows with status 'sent'
        - ``delivered``       — rows with status 'delivered'
        - ``read``            — rows with status 'read'
        - ``failed``          — rows with status 'failed'
        - ``delivered_pct``   — delivered / attempted (0.0 when attempted == 0)
        - ``read_pct``        — read / attempted (0.0 when attempted == 0)
        - ``undeliverable``   — failed rows with error_code == 131026
        - ``marketing_capped``— failed rows with error_code == 131049
        - ``other_failed``    — failed rows with neither 131026 nor 131049
        - ``block_proxy``     — *heuristic* proxy for likely user blocks / spam
                                reports: failed rows whose error_code is neither
                                131026 nor 131049.  This is NOT a definitive
                                measure — Meta does not expose block events
                                directly — but repeated non-undeliverable failures
                                on a number correlate with spam reports.
                                Treat as indicative only.
        - ``campaign``        — echoed back for convenience
        - ``tier``            — echoed back (None if not filtered)
        - ``total_rows``      — all rows for the campaign (including pending)
    """
    base_filter = [WaBroadcastLog.campaign == campaign]
    if tier is not None:
        base_filter.append(WaBroadcastLog.tier == tier)

    # Total rows (including pending)
    total_rows: int = session.execute(
        select(func.count()).where(and_(*base_filter))
    ).scalar_one()

    # Count per status
    status_counts: dict[str, int] = {}
    for status in ("sent", "delivered", "read", "failed"):
        count = session.execute(
            select(func.count()).where(
                and_(*base_filter, WaBroadcastLog.status == status)
            )
        ).scalar_one()
        status_counts[status] = count

    sent = status_counts["sent"]
    delivered = status_counts["delivered"]
    read = status_counts["read"]
    failed = status_counts["failed"]

    # "attempted" = rows that have left 'pending' status
    attempted = sent + delivered + read + failed

    # Percentage guards (avoid ZeroDivisionError)
    delivered_pct = delivered / attempted if attempted else 0.0
    read_pct = read / attempted if attempted else 0.0

    # Per-error-code breakdowns within failed rows
    def _failed_with_code(code: int) -> int:
        return session.execute(
            select(func.count()).where(
                and_(
                    *base_filter,
                    WaBroadcastLog.status == "failed",
                    WaBroadcastLog.error_code == code,
                )
            )
        ).scalar_one()

    undeliverable = _failed_with_code(_CODE_UNDELIVERABLE)
    marketing_capped = _failed_with_code(_CODE_MARKETING_CAP)

    # other_failed = failed rows that are neither undeliverable nor marketing-capped.
    # Must also include rows where error_code IS NULL (locally-failed rows with no
    # Meta code): SQL NULL != anything evaluates to NULL (not TRUE), so without the
    # OR IS NULL predicate those rows are silently excluded from the count.
    from sqlalchemy import or_
    other_failed = session.execute(
        select(func.count()).where(
            and_(
                *base_filter,
                WaBroadcastLog.status == "failed",
                or_(
                    WaBroadcastLog.error_code == None,  # noqa: E711 — SQLAlchemy IS NULL
                    and_(
                        WaBroadcastLog.error_code != _CODE_UNDELIVERABLE,
                        WaBroadcastLog.error_code != _CODE_MARKETING_CAP,
                    ),
                ),
            )
        )
    ).scalar_one()

    # block_proxy: heuristic — failed rows with an error_code that is not
    # 131026 (undeliverable) and not 131049 (marketing cap).  Meta does not
    # surface explicit block signals on the status webhook; recurring
    # non-undeliverable failures are correlated with spam reports in practice.
    # Use for indicative trend analysis only; do NOT act on individual numbers.
    block_proxy = other_failed

    return {
        "campaign": campaign,
        "tier": tier,
        "total_rows": total_rows,
        "attempted": attempted,
        "sent": sent,
        "delivered": delivered,
        "read": read,
        "failed": failed,
        "delivered_pct": delivered_pct,
        "read_pct": read_pct,
        "undeliverable": undeliverable,
        "marketing_capped": marketing_capped,
        "other_failed": other_failed,
        "block_proxy": block_proxy,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_table(summary: dict) -> None:
    """Print a human-readable table for the summary dict."""
    campaign = summary["campaign"]
    tier_label = summary["tier"] or "all tiers"
    print(f"\nBroadcast report — campaign={campaign!r}  tier={tier_label}")
    print("=" * 60)
    print(f"  Total rows (incl. pending) : {summary['total_rows']}")
    print(f"  Attempted (left pending)   : {summary['attempted']}")
    print(f"  Sent                       : {summary['sent']}")
    print(f"  Delivered                  : {summary['delivered']}")
    print(f"  Read                       : {summary['read']}")
    print(f"  Failed                     : {summary['failed']}")
    print("-" * 60)
    print(f"  Delivered %                : {summary['delivered_pct']:.1%}")
    print(f"  Read %                     : {summary['read_pct']:.1%}")
    print("-" * 60)
    print(f"  Undeliverable  (131026)    : {summary['undeliverable']}")
    print(f"  Marketing-capped (131049)  : {summary['marketing_capped']}")
    print(f"  Other failed               : {summary['other_failed']}")
    print(f"  Block proxy (heuristic)    : {summary['block_proxy']}")
    print("=" * 60)
    print()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Print a delivery outcome summary for a broadcast campaign.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--campaign",
        required=True,
        help="Campaign identifier to report on.",
    )
    parser.add_argument(
        "--tier",
        default=None,
        help="Filter by tier (e.g. T1, T2). Omit to report across all tiers.",
    )
    args = parser.parse_args(argv)

    from data_access.engine import get_session  # lazy import — not needed in tests

    with get_session() as session:
        summary = summarize(session, args.campaign, tier=args.tier)

    _print_table(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
