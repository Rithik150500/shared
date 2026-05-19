"""One-time Razorpay setup: create plans + offers for the case-billing launch.

This script is part of the Sub-project E launch sequence (Task 16). It
creates the 5 Razorpay Plans the subscription flow uses and the 2 Offers
the intro-promo + referrer-mutual benefit machinery attaches at
subscription-creation time.

Run order, per `casepilot/docs/GO_LIVE.md`:

    T-7 days: `python -m tools.setup_razorpay_plans_offers --dry-run`
              (verify pricing matches spec Section 1.5)

    T-7 days: `RAZORPAY_KEY_ID=... RAZORPAY_KEY_SECRET=... \
               python -m tools.setup_razorpay_plans_offers`
              (real creation; prints the env-var block to copy into
               the deployment env)

    T-0:      Re-run live mode. Idempotent -- already-created entities
              are detected via name match and skipped; the env-var
              block is re-printed so the T-day script has it.

Plans created (period=monthly|quarterly|yearly, interval=1):

  | name                  | tier      | period    | amount (paise) |
  |-----------------------|-----------|-----------|----------------|
  | advocate_monthly      | advocate  | monthly   | 100,000 (₹1,000) |
  | counsel_monthly       | counsel   | monthly   | 200,000 (₹2,000) |
  | chambers_monthly      | chambers  | monthly   | 400,000 (₹4,000) |
  | chambers_quarterly    | chambers  | quarterly | 1,200,000 (₹12,000) |
  | chambers_yearly       | chambers  | yearly    | 4,800,000 (₹48,000) |

(Pricing per pricing.py / spec Section 1.5. Quarterly = 3*monthly,
yearly = 12*monthly -- no built-in discount on the longer cycles.)

Offers created:

  | name                | percent_off | redemption_type | max_count |
  |---------------------|-------------|-----------------|-----------|
  | chambers_half_off   | 50          | subscription    | 100,000   |
  | counsel_half_off    | 50          | subscription    | 100,000   |

`max_count` is generous: each offer is consumed once per subscriber so
100k is "effectively unlimited" for the launch year.

Idempotency: we fetch the existing Razorpay plans/offers list first and
match by `name` (case-sensitive). If an entity with the target name
exists, we record its id and skip creation. This makes the script safe
to re-run after a partial failure or after re-adding a deleted entity.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Iterable

import httpx


_RAZORPAY_API_BASE = "https://api.razorpay.com"
_TIMEOUT = 30


log = logging.getLogger("setup_razorpay_plans_offers")


# ---------------------------------------------------------------------------
# Plan + offer definitions (single source of truth for the launch sequence)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanSpec:
    """One Razorpay Plan we intend to create."""

    # Stable internal name (matches Razorpay's `item.name` field; used
    # for idempotency lookups).
    name: str
    # Razorpay period: monthly|quarterly|yearly.
    period: str
    # Razorpay interval (1 = "every period"). We only ever use 1.
    interval: int
    # Per-cycle amount in paise (₹1 = 100 paise).
    amount_paise: int
    # Human-readable description (shown on Razorpay dashboard).
    description: str
    # The env-var the deployment uses to point at this plan's id.
    env_var: str


@dataclass(frozen=True)
class OfferSpec:
    """One Razorpay Offer we intend to create."""

    name: str
    percent_off: int
    redemption_type: str  # "subscription" | "payment_link"
    max_count: int
    env_var: str


PLAN_SPECS: list[PlanSpec] = [
    PlanSpec(
        name="advocate_monthly",
        period="monthly",
        interval=1,
        amount_paise=100_000,   # ₹1,000
        description="Nowlez Advocate tier — monthly subscription",
        env_var="RAZORPAY_PLAN_ID_ADVOCATE_MONTHLY",
    ),
    PlanSpec(
        name="counsel_monthly",
        period="monthly",
        interval=1,
        amount_paise=200_000,   # ₹2,000
        description="Nowlez Counsel tier — monthly subscription",
        env_var="RAZORPAY_PLAN_ID_COUNSEL_MONTHLY",
    ),
    PlanSpec(
        name="chambers_monthly",
        period="monthly",
        interval=1,
        amount_paise=400_000,   # ₹4,000
        description="Nowlez Chambers tier — monthly subscription",
        env_var="RAZORPAY_PLAN_ID_CHAMBERS_MONTHLY",
    ),
    PlanSpec(
        # Razorpay's period enum is daily|weekly|monthly|yearly — there is
        # no "quarterly". A quarterly cadence is expressed as monthly with
        # interval=3 (charged every 3 months). The plan name stays
        # "chambers_quarterly" so consumer code references don't need to
        # change.
        name="chambers_quarterly",
        period="monthly",
        interval=3,
        amount_paise=1_200_000,  # 3 × ₹4,000
        description="Nowlez Chambers tier — quarterly subscription (monthly×3)",
        env_var="RAZORPAY_PLAN_ID_CHAMBERS_QUARTERLY",
    ),
    PlanSpec(
        name="chambers_yearly",
        period="yearly",
        interval=1,
        amount_paise=4_800_000,  # 12 × ₹4,000
        description="Nowlez Chambers tier — yearly subscription",
        env_var="RAZORPAY_PLAN_ID_CHAMBERS_YEARLY",
    ),
]


OFFER_SPECS: list[OfferSpec] = [
    OfferSpec(
        name="chambers_half_off",
        percent_off=50,
        redemption_type="subscription",
        max_count=100_000,
        env_var="RAZORPAY_OFFER_ID_CHAMBERS_HALF_OFF",
    ),
    OfferSpec(
        name="counsel_half_off",
        percent_off=50,
        redemption_type="subscription",
        max_count=100_000,
        env_var="RAZORPAY_OFFER_ID_COUNSEL_HALF_OFF",
    ),
]


# ---------------------------------------------------------------------------
# Razorpay HTTP wrappers (lightweight — we do not use the case_billing
# client because that has metric-emission side effects we don't want
# polluting the launch path).
# ---------------------------------------------------------------------------


async def _get(
    path: str, *, key_id: str, key_secret: str, params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT, auth=(key_id, key_secret)) as http:
        resp = await http.get(f"{_RAZORPAY_API_BASE}{path}", params=params)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


async def _post(
    path: str, *, key_id: str, key_secret: str, json_body: dict[str, Any],
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT, auth=(key_id, key_secret)) as http:
        resp = await http.post(f"{_RAZORPAY_API_BASE}{path}", json=json_body)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


# ---------------------------------------------------------------------------
# Plan management
# ---------------------------------------------------------------------------


async def _list_existing_plans(
    *, key_id: str, key_secret: str,
) -> dict[str, str]:
    """Return ``{plan_name: plan_id}`` for every existing plan.

    Razorpay's list endpoint is paginated; we paginate until exhausted.
    """
    result: dict[str, str] = {}
    skip = 0
    while True:
        data = await _get(
            "/v1/plans",
            key_id=key_id, key_secret=key_secret,
            params={"count": 100, "skip": skip},
        )
        items = data.get("items", [])
        for item in items:
            # Razorpay nests the displayed name under item.name.
            inner_item = item.get("item") or {}
            name = inner_item.get("name") or item.get("notes", {}).get("name")
            if name:
                result[name] = item["id"]
        if len(items) < 100:
            break
        skip += 100
    return result


async def _create_plan(spec: PlanSpec, *, key_id: str, key_secret: str) -> str:
    body = {
        "period": spec.period,
        "interval": spec.interval,
        "item": {
            "name": spec.name,
            "amount": spec.amount_paise,
            "currency": "INR",
            "description": spec.description,
        },
        "notes": {
            "managed_by": "case_billing.tools.setup_razorpay_plans_offers",
            "internal_name": spec.name,
        },
    }
    resp = await _post(
        "/v1/plans", key_id=key_id, key_secret=key_secret, json_body=body,
    )
    return resp["id"]


# ---------------------------------------------------------------------------
# Offer management
# ---------------------------------------------------------------------------


async def _list_existing_offers(
    *, key_id: str, key_secret: str,
) -> dict[str, str]:
    """Return ``{offer_name: offer_id}`` for every existing offer."""
    result: dict[str, str] = {}
    skip = 0
    while True:
        data = await _get(
            "/v1/offers",
            key_id=key_id, key_secret=key_secret,
            params={"count": 100, "skip": skip},
        )
        items = data.get("items", [])
        for item in items:
            name = item.get("name")
            if name:
                result[name] = item["id"]
        if len(items) < 100:
            break
        skip += 100
    return result


async def _create_offer(spec: OfferSpec, *, key_id: str, key_secret: str) -> str:
    body = {
        "name": spec.name,
        "percent_off": spec.percent_off,
        "redemption_type": spec.redemption_type,
        "max_count": spec.max_count,
    }
    resp = await _post(
        "/v1/offers", key_id=key_id, key_secret=key_secret, json_body=body,
    )
    return resp["id"]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _run(*, dry_run: bool) -> int:
    if dry_run:
        log.info("DRY-RUN: skipping all Razorpay API calls")
        log.info("Plans that WOULD be created:")
        for p in PLAN_SPECS:
            log.info(
                "  - name=%s period=%s amount=%d paise (=₹%d) env_var=%s",
                p.name, p.period, p.amount_paise, p.amount_paise // 100, p.env_var,
            )
        log.info("Offers that WOULD be created:")
        for o in OFFER_SPECS:
            log.info(
                "  - name=%s percent_off=%d max_count=%d env_var=%s",
                o.name, o.percent_off, o.max_count, o.env_var,
            )
        log.info(
            "DRY-RUN complete -- 5 plans + 2 offers would be created (or skipped if already present)"
        )
        return 0

    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        log.error(
            "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set (got key_id=%s secret_set=%s)",
            bool(key_id), bool(key_secret),
        )
        return 2

    # Reach out and list existing plans + offers once so we can dedupe.
    log.info("Fetching existing plans...")
    try:
        existing_plans = await _list_existing_plans(
            key_id=key_id, key_secret=key_secret,
        )
    except httpx.HTTPError as e:
        log.error("Could not list existing plans: %s", e)
        return 1
    log.info("Razorpay reports %d existing plans on this account", len(existing_plans))

    log.info("Fetching existing offers...")
    existing_offers: dict[str, str] = {}
    offers_supported = True
    try:
        existing_offers = await _list_existing_offers(
            key_id=key_id, key_secret=key_secret,
        )
    except httpx.HTTPStatusError as e:
        # 404 typically means Offers feature isn't enabled for this account
        # tier (Razorpay enables it on request for Subscriptions accounts).
        # We degrade gracefully: skip offer creation entirely; plans still land.
        if e.response is not None and e.response.status_code == 404:
            log.warning(
                "Offers API returned 404 — feature likely not enabled on this "
                "Razorpay account. Skipping offer creation; plans will still be "
                "created. Contact Razorpay support to enable Offers if needed."
            )
            offers_supported = False
        else:
            log.error("Could not list existing offers: %s", e)
            return 1
    except httpx.HTTPError as e:
        log.error("Could not list existing offers: %s", e)
        return 1
    if offers_supported:
        log.info("Razorpay reports %d existing offers on this account", len(existing_offers))

    # Create-or-skip per spec.
    plan_ids: dict[str, str] = {}
    failed = 0
    for spec in PLAN_SPECS:
        if spec.name in existing_plans:
            plan_id = existing_plans[spec.name]
            log.info("SKIP plan (already exists): %s -> %s", spec.name, plan_id)
        else:
            try:
                plan_id = await _create_plan(
                    spec, key_id=key_id, key_secret=key_secret,
                )
            except httpx.HTTPError as e:
                failed += 1
                log.error("FAILED to create plan %s: %s", spec.name, e)
                continue
            log.info("CREATED plan: %s -> %s", spec.name, plan_id)
        plan_ids[spec.env_var] = plan_id

    offer_ids: dict[str, str] = {}
    if not offers_supported:
        log.warning(
            "Skipping all %d offer creations (Offers API not enabled).",
            len(OFFER_SPECS),
        )
    else:
        for spec in OFFER_SPECS:
            if spec.name in existing_offers:
                offer_id = existing_offers[spec.name]
                log.info("SKIP offer (already exists): %s -> %s", spec.name, offer_id)
            else:
                try:
                    offer_id = await _create_offer(
                        spec, key_id=key_id, key_secret=key_secret,
                    )
                except httpx.HTTPError as e:
                    failed += 1
                    log.error("FAILED to create offer %s: %s", spec.name, e)
                    continue
                log.info("CREATED offer: %s -> %s", spec.name, offer_id)
            offer_ids[spec.env_var] = offer_id

    # Print env-var block. The operator pastes this into the deployment
    # env exactly as-is.
    print("\n" + "=" * 78)
    print("BillingConfig env-var block — paste into the deployment env:")
    print("=" * 78)
    for env_var in sorted(plan_ids):
        print(f"{env_var}={plan_ids[env_var]}")
    for env_var in sorted(offer_ids):
        print(f"{env_var}={offer_ids[env_var]}")
    print("=" * 78 + "\n")

    log.info(
        "Summary: plans_processed=%d offers_processed=%d failed=%d",
        len(plan_ids), len(offer_ids), failed,
    )
    return 0 if failed == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="setup_razorpay_plans_offers",
        description="One-time Razorpay setup: create case-billing plans + offers.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print would-be operations; skip Razorpay API calls.",
    )
    return p


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    return asyncio.run(_run(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
