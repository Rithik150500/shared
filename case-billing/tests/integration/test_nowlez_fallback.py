"""Day-31 fallback to Munshi tests (Task 7.5.2).

`fallback_to_munshi(user_id, session, send_template_fn)` runs on day 31
when the trial has expired and the user has not picked a tier:

  today_ist = date.today()
  INSERT INTO users_munshi (user_id, billing_anniversary_date=today)
       ON CONFLICT DO NOTHING
  FOR each active case with refresh_enabled=True:
      INSERT case_billing_periods (period_start=NOW(), period_end=NULL)
  IF user has > 200 active cases:
      pause oldest down to 200 (refresh_enabled=False; close periods)
  send `nowlez_trial_fallback_v1` template
  audit log 'nowlez.trial_fallback_to_munshi'

`freeze_account` is the alternative path (BillingConfig
`nowlez_lapsed_trial_action='freeze_account'`).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from case_billing.nowlez.fallback import (
    LAPSED_TRIAL_ACTION_FALLBACK,
    LAPSED_TRIAL_ACTION_FREEZE,
    apply_lapsed_trial_action,
    fallback_to_munshi,
    freeze_account,
)


@pytest.fixture()
def session() -> Session:
    from data_access.base import Base

    import data_access.models.auth  # noqa: F401
    import data_access.models.billing  # noqa: F401
    import data_access.models.case  # noqa: F401
    import data_access.models.user  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _make_expired_trial_user(session: Session) -> uuid.UUID:
    from data_access.models.user import User, UserNowlez

    user = User(phone=f"+91999{uuid.uuid4().hex[:7]}")
    session.add(user)
    session.flush()
    session.add(
        UserNowlez(
            user_id=user.id, name="Trial",
            tier=None,
            trial_started_at=datetime.now(timezone.utc) - timedelta(days=31),
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    session.flush()
    return user.id


def _add_cases(
    session: Session,
    user_id: uuid.UUID,
    n: int,
    *,
    refresh_enabled: bool = True,
) -> list[uuid.UUID]:
    from data_access.models.case import Case

    ids = []
    for _ in range(n):
        c = Case(
            user_id=user_id, cnr=uuid.uuid4().hex[:16],
            portal="district", refresh_enabled=refresh_enabled,
        )
        session.add(c)
        session.flush()
        ids.append(c.id)
    return ids


# --- fallback_to_munshi ----------------------------------------------------


def test_fallback_creates_munshi_extension_and_opens_periods(
    session: Session,
) -> None:
    user_id = _make_expired_trial_user(session)
    _add_cases(session, user_id, n=50)

    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.user import UserMunshi, UserNowlez

    munshi = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one()
    assert munshi.billing_anniversary_date is not None

    periods = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.user_id == user_id)
    ).scalars().all()
    assert len(periods) == 50

    sender.assert_called_once()
    assert sender.call_args.kwargs["template"] == "nowlez_trial_fallback_v1"


def test_fallback_caps_at_200_cases(session: Session) -> None:
    user_id = _make_expired_trial_user(session)
    _add_cases(session, user_id, n=250)

    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.case import Case

    # Only 200 cases remain active.
    active = session.execute(
        select(Case)
        .where(Case.user_id == user_id)
        .where(Case.refresh_enabled.is_(True))
    ).scalars().all()
    assert len(active) == 200

    paused = session.execute(
        select(Case)
        .where(Case.user_id == user_id)
        .where(Case.refresh_enabled.is_(False))
    ).scalars().all()
    assert len(paused) == 50

    # 200 case_billing_periods created (one per active case).
    open_periods = session.execute(
        select(CaseBillingPeriod)
        .where(CaseBillingPeriod.user_id == user_id)
        .where(CaseBillingPeriod.period_end.is_(None))
    ).scalars().all()
    assert len(open_periods) == 200


def test_fallback_keeps_tier_null(session: Session) -> None:
    user_id = _make_expired_trial_user(session)
    _add_cases(session, user_id, n=10)
    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    from data_access.models.user import UserNowlez
    nowlez = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one()
    assert nowlez.tier is None


def test_fallback_idempotent_on_existing_munshi_extension(
    session: Session,
) -> None:
    """If users_munshi already exists, do not overwrite billing_anniversary_date."""
    from data_access.models.user import UserMunshi

    user_id = _make_expired_trial_user(session)
    _add_cases(session, user_id, n=3)
    session.add(UserMunshi(
        user_id=user_id, billing_anniversary_date=date(2025, 1, 1),
    ))
    session.flush()

    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    munshi = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one()
    assert munshi.billing_anniversary_date == date(2025, 1, 1)


# --- freeze_account --------------------------------------------------------


def test_freeze_account_disables_refresh_and_sends_template(
    session: Session,
) -> None:
    user_id = _make_expired_trial_user(session)
    _add_cases(session, user_id, n=10)

    sender = AsyncMock()
    _run(freeze_account(user_id, session, send_template_fn=sender))
    session.flush()

    from data_access.models.case import Case
    cases = session.execute(
        select(Case).where(Case.user_id == user_id)
    ).scalars().all()
    assert all(c.refresh_enabled is False for c in cases)
    sender.assert_called_once()
    assert sender.call_args.kwargs["template"] == "nowlez_trial_fallback_v1"


# --- Task 14 edge cases ----------------------------------------------------


def test_fallback_zero_cases_skips_billing_and_sends_no_billing_template(
    session: Session,
) -> None:
    """User has 0 active cases at day 31 → no Munshi extension, no
    billing periods, no fallback template. Only the no-billing template
    + an audit row."""
    from data_access.models.audit import AuditLog
    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.user import UserMunshi

    user_id = _make_expired_trial_user(session)
    # No cases added.

    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    munshi = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one_or_none()
    assert munshi is None, "should not create Munshi extension for zero-case user"

    periods = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.user_id == user_id)
    ).scalars().all()
    assert periods == []

    sender.assert_called_once()
    assert (
        sender.call_args.kwargs["template"] == "nowlez_trial_ended_no_billing_v1"
    )

    audit = session.execute(
        select(AuditLog).where(AuditLog.user_id == user_id)
    ).scalars().all()
    assert any(
        a.event_type == "nowlez.trial_fallback_noop"
        and a.metadata_.get("reason") == "no_active_cases"
        for a in audit
    )


def test_fallback_user_with_paid_tier_and_active_sub_is_noop(
    session: Session,
) -> None:
    """User raced into a paid tier AND has an active subscription
    between cron pickup and execution → no Munshi extension created,
    no template sent, only audit.

    A bare tier= without an active subscription is a different scenario
    (cancellation hand-off) and IS allowed to fall through — see
    `test_fallback_proceeds_when_tier_set_but_subscription_cancelled`.
    """
    from data_access.models.audit import AuditLog
    from data_access.models.billing import (
        CaseBillingPeriod, Subscription,
    )
    from data_access.models.user import UserMunshi, UserNowlez

    user_id = _make_expired_trial_user(session)
    nowlez = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one()
    nowlez.tier = "counsel"
    session.flush()
    # Active subscription is the key signal — without it, fallback should
    # proceed because the cancellation hand-off needs to work.
    session.add(Subscription(
        user_id=user_id, tier="counsel", billing_cycle="monthly",
        razorpay_subscription_id="sub_racing",
        status="active",
        intro_promo_state="in_intro", referral_state="no_referral",
    ))
    session.flush()
    _add_cases(session, user_id, n=20)

    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    # No Munshi extension; no billing periods; no template.
    munshi = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one_or_none()
    assert munshi is None
    periods = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.user_id == user_id)
    ).scalars().all()
    assert periods == []
    sender.assert_not_called()

    # Audit row records the bail-out.
    audit = session.execute(
        select(AuditLog).where(AuditLog.user_id == user_id)
    ).scalars().all()
    assert any(
        a.event_type == "nowlez.trial_fallback_noop"
        and a.metadata_.get("reason") == "user_has_paid_tier"
        and a.metadata_.get("tier") == "counsel"
        for a in audit
    )


def test_fallback_proceeds_when_tier_set_but_subscription_cancelled(
    session: Session,
) -> None:
    """The webhook router calls `fallback_to_munshi` after a
    `subscription.cancelled` event fires. At that point `users_nowlez.tier`
    still says e.g. 'counsel' (the cancellation flow doesn't null it),
    but the subscription row's status is 'cancelled'. The fallback must
    proceed in that case so the user keeps service via Munshi.
    """
    from data_access.models.billing import (
        CaseBillingPeriod, Subscription,
    )
    from data_access.models.user import UserMunshi, UserNowlez

    user_id = _make_expired_trial_user(session)
    nowlez = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one()
    nowlez.tier = "counsel"
    session.flush()
    session.add(Subscription(
        user_id=user_id, tier="counsel", billing_cycle="monthly",
        razorpay_subscription_id="sub_already_cancelled",
        status="cancelled",
        intro_promo_state="past_intro", referral_state="no_referral",
    ))
    session.flush()
    _add_cases(session, user_id, n=5)

    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    munshi = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one()
    assert munshi.billing_anniversary_date is not None

    periods = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.user_id == user_id)
    ).scalars().all()
    assert len(periods) == 5

    sender.assert_called_once()
    assert sender.call_args.kwargs["template"] == "nowlez_trial_fallback_v1"


def test_fallback_reuses_existing_munshi_extension_with_active_cases(
    session: Session,
) -> None:
    """User who already has a Munshi extension at day 31 → reuse it,
    don't overwrite billing_anniversary_date, and open billing periods
    for active cases (the transition from trial to active Munshi)."""
    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.user import UserMunshi

    user_id = _make_expired_trial_user(session)
    original_anniversary = date(2025, 6, 15)
    session.add(UserMunshi(
        user_id=user_id, billing_anniversary_date=original_anniversary,
    ))
    session.flush()
    case_ids = _add_cases(session, user_id, n=12)

    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    # Anniversary preserved (no overwrite).
    munshi = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one()
    assert munshi.billing_anniversary_date == original_anniversary

    # Billing periods opened for every active case.
    periods = session.execute(
        select(CaseBillingPeriod).where(CaseBillingPeriod.user_id == user_id)
    ).scalars().all()
    assert len(periods) == len(case_ids)
    sender.assert_called_once()
    assert sender.call_args.kwargs["template"] == "nowlez_trial_fallback_v1"


def test_fallback_does_not_overwrite_already_open_billing_periods(
    session: Session,
) -> None:
    """If a case already has an open billing_period (e.g. the user was
    in fallback Munshi before, was reactivated to Nowlez paid, then
    cancelled and lapsed again), the function must not double-open."""
    from data_access.models.billing import CaseBillingPeriod
    from data_access.models.user import UserMunshi

    user_id = _make_expired_trial_user(session)
    session.add(UserMunshi(user_id=user_id, billing_anniversary_date=date.today()))
    session.flush()
    case_ids = _add_cases(session, user_id, n=5)
    # Pre-open a period on the first case.
    pre_existing = CaseBillingPeriod(
        user_id=user_id, case_id=case_ids[0],
        period_start=datetime.now(timezone.utc) - timedelta(days=5),
        period_end=None,
    )
    session.add(pre_existing)
    session.flush()

    sender = AsyncMock()
    _run(fallback_to_munshi(user_id, session, send_template_fn=sender))
    session.flush()

    # Exactly N periods total, not N+1; the first case kept its old one.
    periods_for_first = session.execute(
        select(CaseBillingPeriod)
        .where(CaseBillingPeriod.case_id == case_ids[0])
    ).scalars().all()
    assert len(periods_for_first) == 1
    # SQLite round-trips UUIDs as strings, so cast both sides.
    assert str(periods_for_first[0].id) == str(pre_existing.id)


def test_freeze_account_with_paid_tier_and_active_sub_is_noop(
    session: Session,
) -> None:
    """If the user raced into paid tier AND has an active subscription,
    freeze must NOT disable their cases — they're now a paying customer."""
    from data_access.models.audit import AuditLog
    from data_access.models.billing import Subscription
    from data_access.models.case import Case
    from data_access.models.user import UserNowlez

    user_id = _make_expired_trial_user(session)
    nowlez = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one()
    nowlez.tier = "chambers"
    session.flush()
    session.add(Subscription(
        user_id=user_id, tier="chambers", billing_cycle="monthly",
        razorpay_subscription_id="sub_chambers_active",
        status="active",
        intro_promo_state="in_intro", referral_state="no_referral",
    ))
    session.flush()
    _add_cases(session, user_id, n=4)

    sender = AsyncMock()
    _run(freeze_account(user_id, session, send_template_fn=sender))
    session.flush()

    # Cases stay enabled.
    cases = session.execute(
        select(Case).where(Case.user_id == user_id)
    ).scalars().all()
    assert all(c.refresh_enabled is True for c in cases)
    sender.assert_not_called()

    # Audit shows the noop.
    audit = session.execute(
        select(AuditLog).where(AuditLog.user_id == user_id)
    ).scalars().all()
    assert any(
        a.event_type == "nowlez.trial_freeze_noop"
        and a.metadata_.get("tier") == "chambers"
        for a in audit
    )


def test_freeze_account_explicitly_nulls_legacy_free_tier(
    session: Session,
) -> None:
    """The freeze path must null out a stale 'free' tier so subsequent
    eligibility checks treat the user as having no Nowlez access."""
    from data_access.models.user import UserNowlez

    user_id = _make_expired_trial_user(session)
    nowlez = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one()
    nowlez.tier = "free"
    session.flush()
    _add_cases(session, user_id, n=2)

    sender = AsyncMock()
    _run(freeze_account(user_id, session, send_template_fn=sender))
    session.flush()

    nowlez = session.execute(
        select(UserNowlez).where(UserNowlez.user_id == user_id)
    ).scalar_one()
    assert nowlez.tier is None


# --- apply_lapsed_trial_action dispatcher ----------------------------------


def test_apply_action_dispatches_to_fallback_to_munshi(
    session: Session,
) -> None:
    user_id = _make_expired_trial_user(session)
    _add_cases(session, user_id, n=3)
    sender = AsyncMock()

    result = _run(apply_lapsed_trial_action(
        user_id, session, sender,
        action=LAPSED_TRIAL_ACTION_FALLBACK,
    ))
    session.flush()
    assert result == LAPSED_TRIAL_ACTION_FALLBACK

    from data_access.models.user import UserMunshi
    munshi = session.execute(
        select(UserMunshi).where(UserMunshi.user_id == user_id)
    ).scalar_one()
    assert munshi.billing_anniversary_date is not None


def test_apply_action_dispatches_to_freeze_account(session: Session) -> None:
    user_id = _make_expired_trial_user(session)
    _add_cases(session, user_id, n=3)
    sender = AsyncMock()

    result = _run(apply_lapsed_trial_action(
        user_id, session, sender,
        action=LAPSED_TRIAL_ACTION_FREEZE,
    ))
    session.flush()
    assert result == LAPSED_TRIAL_ACTION_FREEZE

    from data_access.models.case import Case
    cases = session.execute(
        select(Case).where(Case.user_id == user_id)
    ).scalars().all()
    assert all(c.refresh_enabled is False for c in cases)


def test_apply_action_unknown_value_raises_value_error(
    session: Session,
) -> None:
    user_id = _make_expired_trial_user(session)
    sender = AsyncMock()
    with pytest.raises(ValueError, match="Unknown nowlez_lapsed_trial_action"):
        _run(apply_lapsed_trial_action(
            user_id, session, sender, action="self_destruct",
        ))
