"""Razorpay Subscriptions API wrapper.

Covers the four operations the Nowlez billing flow needs:

  * :func:`create_subscription` — start a new monthly/quarterly/yearly plan.
  * :func:`cancel_subscription` — cancel (default at cycle end so the user
    keeps paid-for service through their billed period).
  * :func:`update_subscription_offer` — attach the referrer-mutual offer to an
    existing subscription (Section 5.5 fallback path).
  * :func:`fetch_subscription` — reconcile local row state with Razorpay's.

All return a :class:`RazorpaySubscription` dataclass that exposes only the
fields the spec actually persists in our database, so adding new Razorpay
response fields in the future does not require schema migrations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from case_billing.razorpay_client.client import RazorpayHTTPClient


@dataclass(frozen=True)
class RazorpaySubscription:
    """The slice of the Razorpay subscription object we persist locally."""

    id: str
    short_url: str
    status: str
    current_start: int | None
    current_end: int | None
    notes: dict[str, Any]


def _parse(payload: dict[str, Any]) -> RazorpaySubscription:
    return RazorpaySubscription(
        id=payload["id"],
        short_url=payload.get("short_url", ""),
        status=payload.get("status", ""),
        current_start=payload.get("current_start"),
        current_end=payload.get("current_end"),
        notes=dict(payload.get("notes") or {}),
    )


async def create_subscription(
    client: RazorpayHTTPClient,
    *,
    plan_id: str,
    customer_notify: bool,
    total_count: int,
    notes: dict[str, Any],
    offer_id: str | None = None,
) -> RazorpaySubscription:
    """POST /v1/subscriptions.

    Args:
        client: A configured :class:`RazorpayHTTPClient`.
        plan_id: Razorpay plan_id (one of the env-supplied plan IDs).
        customer_notify: If True, Razorpay sends invoice emails directly.
        total_count: Number of billing cycles before auto-end (per spec).
        notes: Free-form metadata persisted on the Razorpay object; use it to
            stash our internal user_id / tier / referral_code so the webhook
            handler can correlate events without a DB lookup.
        offer_id: Optional Razorpay offer_id to attach (intro promo or
            referrer-mutual discount). Omitted from the payload when None.
    """
    body: dict[str, Any] = {
        "plan_id": plan_id,
        "customer_notify": 1 if customer_notify else 0,
        "total_count": total_count,
        "notes": notes,
    }
    if offer_id is not None:
        body["offer_id"] = offer_id
    payload = await client._request("POST", "/v1/subscriptions", json_data=body)
    return _parse(payload)


async def cancel_subscription(
    client: RazorpayHTTPClient,
    subscription_id: str,
    cancel_at_cycle_end: bool = True,
) -> RazorpaySubscription:
    """POST /v1/subscriptions/{id}/cancel.

    Defaults to cancel-at-cycle-end so the customer keeps the service through
    the period they already paid for; pass ``False`` only for refund/abuse
    flows where immediate termination is required.
    """
    body = {"cancel_at_cycle_end": 1 if cancel_at_cycle_end else 0}
    payload = await client._request(
        "POST",
        f"/v1/subscriptions/{subscription_id}/cancel",
        json_data=body,
    )
    return _parse(payload)


async def update_subscription_offer(
    client: RazorpayHTTPClient,
    subscription_id: str,
    offer_id: str,
) -> RazorpaySubscription:
    """PATCH /v1/subscriptions/{id} to attach a new offer mid-cycle.

    Used by the referrer-mutual-benefit fallback (spec Section 5.5): when a
    new referral completes during an active subscription, we attach the
    mutual-benefit offer so the next cycle is discounted automatically.
    """
    payload = await client._request(
        "PATCH",
        f"/v1/subscriptions/{subscription_id}",
        json_data={"offer_id": offer_id},
    )
    return _parse(payload)


async def fetch_subscription(
    client: RazorpayHTTPClient,
    subscription_id: str,
) -> RazorpaySubscription:
    """GET /v1/subscriptions/{id} for reconciliation against our local row."""
    payload = await client._request("GET", f"/v1/subscriptions/{subscription_id}")
    return _parse(payload)
