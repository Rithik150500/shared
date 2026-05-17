"""Referral state-machine tests (Task 7.3.2 — 7.3.4).

Referral lifecycle (kept compatible with the existing
`referrals.state IN ('pending', 'mutual_applied', 'expired')` constraint
plus the richer `subscriptions.referral_state` state machine):

    Referral row:
        pending  ── apply_referree_2nd_month_discount ──→ mutual_applied
        pending  ── expire                            ──→ expired

    subscriptions.referral_state (referree side):
        no_referral
          ── find_or_create_referral applied ──→ pending_mutual
          ── apply_referree_2nd_month_discount ──→ mutual_applied
          ── expire ──→ expired

Advocate exclusion (spec FAQ Q9): when *either* side is on the advocate
tier, neither side earns the referral mutual discount.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.errors import BillingError
from case_billing.nowlez.referrals import (
    apply_referree_2nd_month_discount,
    find_or_create_referral,
    schedule_referrer_mutual_benefit,
    expire_referral,
    mark_referrer_mutual_applied,
)


@pytest.fixture()
def session() -> Session:
    from data_access.base import Base

    import data_access.models.auth  # noqa: F401
    import data_access.models.billing  # noqa: F401
    import data_access.models.user  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _make_user_with_nowlez(
    session: Session,
    *,
    tier: str | None = None,
    referral_code: str | None = None,
) -> uuid.UUID:
    from data_access.models.user import User, UserNowlez

    user = User(phone=f"+91999{uuid.uuid4().hex[:7]}")
    session.add(user)
    session.flush()
    session.add(
        UserNowlez(
            user_id=user.id, name="X", tier=tier,
            referral_code=referral_code or uuid.uuid4().hex[:8],
        )
    )
    session.flush()
    return user.id


def _make_subscription(
    session: Session,
    *,
    user_id: uuid.UUID,
    tier: str = "chambers",
    referral_state: str = "no_referral",
) -> uuid.UUID:
    from data_access.models.billing import Subscription

    sub = Subscription(
        user_id=user_id, tier=tier, billing_cycle="monthly",
        status="active", intro_promo_state="past_intro",
        referral_state=referral_state,
    )
    session.add(sub)
    session.flush()
    return sub.id


# --- find_or_create_referral ----------------------------------------------


def test_find_or_create_unknown_code_returns_none(session: Session) -> None:
    referred = _make_user_with_nowlez(session)
    out = _run(find_or_create_referral(
        referred_user_id=referred,
        referral_code="DOES_NOT_EXIST",
        session=session,
    ))
    assert out is None


def test_find_or_create_self_referral_returns_none(session: Session) -> None:
    user_id = _make_user_with_nowlez(session, referral_code="MYCODE")
    out = _run(find_or_create_referral(
        referred_user_id=user_id, referral_code="MYCODE", session=session,
    ))
    assert out is None


def test_find_or_create_creates_referral_row(session: Session) -> None:
    referrer = _make_user_with_nowlez(session, referral_code="REFERRER1")
    referred = _make_user_with_nowlez(session)

    referral = _run(find_or_create_referral(
        referred_user_id=referred,
        referral_code="REFERRER1",
        session=session,
    ))
    session.flush()

    assert referral is not None
    from data_access.models.billing import Referral
    rows = session.execute(
        select(Referral).where(Referral.referred_user_id == referred)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].state == "pending"


def test_find_or_create_idempotent(session: Session) -> None:
    referrer = _make_user_with_nowlez(session, referral_code="REFCODE2")
    referred = _make_user_with_nowlez(session)

    first = _run(find_or_create_referral(
        referred_user_id=referred, referral_code="REFCODE2", session=session,
    ))
    second = _run(find_or_create_referral(
        referred_user_id=referred, referral_code="REFCODE2", session=session,
    ))
    session.flush()

    assert first is not None
    assert second is not None
    # str() normalizes UUID vs str (SQLite drops the UUID type on round-trip).
    assert str(first.id) == str(second.id)


# --- apply_referree_2nd_month_discount ------------------------------------


def test_apply_referree_2nd_month_discount_chambers_chambers(
    session: Session,
) -> None:
    """Chambers referrer + Chambers referree → referree's 2nd month half-off."""
    referrer = _make_user_with_nowlez(
        session, tier="chambers", referral_code="CHAMBREF",
    )
    referred = _make_user_with_nowlez(session, tier="chambers")
    referred_sub = _make_subscription(
        session, user_id=referred, tier="chambers",
        referral_state="pending_mutual",
    )
    _run(find_or_create_referral(
        referred_user_id=referred, referral_code="CHAMBREF", session=session,
    ))
    session.flush()

    _run(apply_referree_2nd_month_discount(referred_sub, session))
    session.flush()

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.id == referred_sub)
    ).scalar_one()
    assert sub.referral_state == "mutual_applied"


def test_apply_referree_2nd_month_advocate_referree_no_discount(
    session: Session,
) -> None:
    """Advocate referree → no discount applied (excluded per Q9)."""
    referrer = _make_user_with_nowlez(
        session, tier="chambers", referral_code="CHAMBREF2",
    )
    referred = _make_user_with_nowlez(session, tier="advocate")
    referred_sub = _make_subscription(
        session, user_id=referred, tier="advocate",
        referral_state="pending_mutual",
    )
    _run(find_or_create_referral(
        referred_user_id=referred, referral_code="CHAMBREF2", session=session,
    ))
    session.flush()

    # Apply should be a no-op for advocate referree (no state change).
    _run(apply_referree_2nd_month_discount(referred_sub, session))
    session.flush()

    from data_access.models.billing import Subscription
    sub = session.execute(
        select(Subscription).where(Subscription.id == referred_sub)
    ).scalar_one()
    # Advocate is excluded → stays in pending_mutual.
    assert sub.referral_state == "pending_mutual"


# --- schedule_referrer_mutual_benefit + mark_referrer_mutual_applied ------


def test_schedule_then_mark_referrer_mutual_chambers_chambers(
    session: Session,
) -> None:
    """Non-advocate pair: referrer earns cycle-2 mutual benefit."""
    referrer = _make_user_with_nowlez(
        session, tier="chambers", referral_code="CMUT1",
    )
    referred = _make_user_with_nowlez(session, tier="chambers")
    referrer_sub = _make_subscription(
        session, user_id=referrer, tier="chambers",
    )
    referral = _run(find_or_create_referral(
        referred_user_id=referred, referral_code="CMUT1", session=session,
    ))
    session.flush()

    _run(schedule_referrer_mutual_benefit(referral.id, session))
    _run(mark_referrer_mutual_applied(referral.id, session))
    session.flush()

    from data_access.models.billing import Referral
    r = session.execute(
        select(Referral).where(Referral.id == referral.id)
    ).scalar_one()
    assert r.state == "mutual_applied"


def test_advocate_referrer_excluded(session: Session) -> None:
    """Advocate referrer + Chambers referree → referrer earns nothing."""
    referrer = _make_user_with_nowlez(
        session, tier="advocate", referral_code="ADVREF",
    )
    referred = _make_user_with_nowlez(session, tier="chambers")
    referral = _run(find_or_create_referral(
        referred_user_id=referred, referral_code="ADVREF", session=session,
    ))
    session.flush()

    # schedule should no-op for advocate referrer.
    _run(schedule_referrer_mutual_benefit(referral.id, session))
    session.flush()

    from data_access.models.billing import Referral
    r = session.execute(
        select(Referral).where(Referral.id == referral.id)
    ).scalar_one()
    # State stays 'pending'.
    assert r.state == "pending"


def test_advocate_advocate_no_mutual(session: Session) -> None:
    """Advocate + Advocate: neither side gets anything."""
    referrer = _make_user_with_nowlez(
        session, tier="advocate", referral_code="ADVADV",
    )
    referred = _make_user_with_nowlez(session, tier="advocate")
    referred_sub = _make_subscription(
        session, user_id=referred, tier="advocate",
        referral_state="pending_mutual",
    )
    referral = _run(find_or_create_referral(
        referred_user_id=referred, referral_code="ADVADV", session=session,
    ))
    session.flush()

    _run(apply_referree_2nd_month_discount(referred_sub, session))
    _run(schedule_referrer_mutual_benefit(referral.id, session))
    session.flush()

    from data_access.models.billing import Referral, Subscription
    r = session.execute(
        select(Referral).where(Referral.id == referral.id)
    ).scalar_one()
    sub = session.execute(
        select(Subscription).where(Subscription.id == referred_sub)
    ).scalar_one()
    assert r.state == "pending"
    assert sub.referral_state == "pending_mutual"


# --- expire_referral -------------------------------------------------------


def test_expire_referral_transitions_state(session: Session) -> None:
    referrer = _make_user_with_nowlez(
        session, tier="counsel", referral_code="EXPCODE",
    )
    referred = _make_user_with_nowlez(session, tier="counsel")
    referral = _run(find_or_create_referral(
        referred_user_id=referred, referral_code="EXPCODE", session=session,
    ))
    session.flush()

    _run(expire_referral(referral.id, session))
    session.flush()

    from data_access.models.billing import Referral
    r = session.execute(
        select(Referral).where(Referral.id == referral.id)
    ).scalar_one()
    assert r.state == "expired"
