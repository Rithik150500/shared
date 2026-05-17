"""High-level multi-product billing predicates (Task 13).

A single user can simultaneously hold Munshi (postpaid case tracking)
and Nowlez (subscription) extensions. Before the Munshi anniversary cron
charges them ₹10 × case_count, it MUST verify the user isn't already
covered by an active Nowlez paid tier *or* a Nowlez trial window — both
include Munshi case tracking and should suppress the postpaid bill.

The lower-level predicate ``is_user_eligible_for_munshi_billing`` (see
:mod:`case_billing.shared.eligibility`) already handles each of those
conditions inline, but the spec (Section 13.1) calls for an *explicit*
top-level wrapper so call sites communicate intent — the cron isn't
checking "is the user a valid Munshi candidate" so much as it is asking
"given the cross-product state right now, should we charge this user
Munshi-style this cycle?".

The wrapper :func:`should_charge_munshi_for_this_case` is that gate.
It is intentionally a thin facade over the existing predicate plus the
two Nowlez-overrides; we keep the underlying checks centralised in
``eligibility.py`` so the truth-table only lives in one place.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from case_billing.shared.eligibility import (
    has_nowlez_access,
    is_paying_nowlez_subscriber,
    is_user_eligible_for_munshi_billing,
)


async def should_charge_munshi_for_this_case(
    user_id: uuid.UUID, session: Session,
) -> bool:
    """Return True iff the Munshi cron should bill this user this cycle.

    Spec Section 13.1: explicit cross-product gate that wraps three
    independent checks the cron would otherwise have to repeat at every
    call site:

    1. **User has Munshi extension and isn't suspended** — delegated to
       :func:`case_billing.shared.eligibility.is_user_eligible_for_munshi_billing`
       which covers the user-active, has-munshi-row, and latest-invoice-
       not-suspended cases.
    2. **User is NOT on an active paid Nowlez tier** — if they are, the
       Nowlez subscription bills them and Munshi must stay quiet.
    3. **User is NOT inside an active Nowlez trial** — the 30-day
       chambers trial covers Munshi case tracking; charging during the
       trial would double-bill.

    All three must hold for the function to return True. The order is
    important for short-circuit efficiency: the eligibility predicate
    already short-circuits on the Nowlez conditions internally, but we
    re-check explicitly so this function reads as the canonical
    cross-product decision point regardless of whether eligibility's
    implementation evolves.

    Args:
        user_id: User to evaluate.
        session: Open SQLAlchemy session bound to the billing DB.

    Returns:
        True if the Munshi cron should proceed to bill this cycle;
        False otherwise (Nowlez-covered, ineligible, or suspended).
    """
    # 1. Lower-level Munshi-side checks (active, has extension, not
    # suspended). This also internally short-circuits on Nowlez state,
    # but we re-assert below so this wrapper is observably the gate.
    if not await is_user_eligible_for_munshi_billing(user_id, session):
        return False

    # 2. Paid Nowlez subscriber → Nowlez bills them; Munshi stays quiet.
    if await is_paying_nowlez_subscriber(user_id, session):
        return False

    # 3. Active Nowlez trial (tier=NULL but trial_ends_at in future) →
    # trial covers case tracking; suppress Munshi.
    #
    # has_nowlez_access returns True for both paid tier and active trial;
    # we already filtered the paid case above, so a True here narrowly
    # means "in active trial".
    if await has_nowlez_access(user_id, session):
        return False

    return True


__all__ = ["should_charge_munshi_for_this_case"]
