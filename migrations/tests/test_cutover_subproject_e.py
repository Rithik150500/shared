"""Tests for ``migrations.cutover_subproject_e`` (Sub-project E Task 17.1).

Covers:

* ``_backfill_subscriptions`` inserts one row per legacy Razorpay-paying
  user with the right state-machine defaults.
* ``_reset_free_tier_to_trial`` rewrites ``tier='free'`` to NULL +
  fresh 30-day trial window.
* ``_verify_munshi_anniversary`` returns 0 for the happy path and is
  observed (RuntimeError) when missing.
* Idempotency: running ``run_cutover`` twice produces the same final
  state — second run is a no-op (counts == 0).
* ``--dry-run`` mode reports counts but commits nothing.
* E-2: ``_verify_munshi_anniversary`` runs as a pre-flight gate; a
  positive count aborts before any writes (no backfill, no free-tier
  reset).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from data_access.models.billing import Subscription
from data_access.models.user import User, UserMunshi, UserNowlez
from migrations import cutover_subproject_e as _cutover_mod
from migrations.cutover_subproject_e import (
    CutoverPreflightFailed,
    REFERRAL_STATE_HAS_REFERRER,
    REFERRAL_STATE_NO_REFERRER,
    INTRO_PROMO_STATE_BACKFILL,
    NOWLEZ_TRIAL_DURATION_DAYS,
    run_cutover,
)


# Fixed instant so the trial-window math is deterministic across re-runs.
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_user(db_session, *, phone: str | None = None, email: str | None = None):
    u = User(phone=phone, email=email or f"{uuid.uuid4().hex[:8]}@example.com")
    db_session.add(u)
    db_session.flush()
    return u


def _attach_nowlez(
    db_session,
    user: User,
    *,
    tier: str | None = None,
    razorpay_customer_id: str | None = None,
    razorpay_subscription_id: str | None = None,
    referred_by: uuid.UUID | None = None,
    name: str = "test",
) -> UserNowlez:
    n = UserNowlez(
        user_id=user.id,
        name=name,
        tier=tier,
        razorpay_customer_id=razorpay_customer_id,
        razorpay_subscription_id=razorpay_subscription_id,
        referred_by=referred_by,
    )
    db_session.add(n)
    db_session.flush()
    return n


def _attach_munshi(db_session, user: User, *, anniversary: date | None) -> UserMunshi:
    m = UserMunshi(user_id=user.id, billing_anniversary_date=anniversary)
    db_session.add(m)
    db_session.flush()
    return m


# ---------------------------------------------------------------------------
# Step 1 — subscriptions backfill
# ---------------------------------------------------------------------------


def test_backfill_inserts_one_subscription_per_legacy_paying_user(db_session):
    u = _make_user(db_session, phone="+91PAYER")
    _attach_nowlez(
        db_session, u,
        tier="counsel",
        razorpay_customer_id="cust_legacy_1",
        razorpay_subscription_id="sub_legacy_1",
    )
    db_session.commit()

    summary = run_cutover(db_session, now=_NOW)

    assert summary["subscriptions_inserted"] == 1
    subs = db_session.query(Subscription).all()
    assert len(subs) == 1
    s = subs[0]
    # SQLite stores UUID as TEXT; compare as strings so the test runs on
    # both dialects without conditional logic.
    assert str(s.user_id) == str(u.id)
    assert s.tier == "counsel"
    assert s.billing_cycle == "monthly"
    assert s.razorpay_customer_id == "cust_legacy_1"
    assert s.razorpay_subscription_id == "sub_legacy_1"
    assert s.status == "active"
    assert s.intro_promo_state == INTRO_PROMO_STATE_BACKFILL
    assert s.referral_state == REFERRAL_STATE_NO_REFERRER


def test_backfill_sets_referral_state_when_referred_by_is_set(db_session):
    referrer = _make_user(db_session, phone="+91REF1")
    referee = _make_user(db_session, phone="+91REF2")
    _attach_nowlez(
        db_session, referee,
        tier="chambers",
        razorpay_customer_id="cust_ref_2",
        razorpay_subscription_id="sub_ref_2",
        referred_by=referrer.id,
    )
    db_session.commit()

    run_cutover(db_session, now=_NOW)

    sub = db_session.query(Subscription).one()
    assert sub.referral_state == REFERRAL_STATE_HAS_REFERRER


def test_backfill_skips_users_without_razorpay_customer_id(db_session):
    """Munshi-only / never-subscribed users → no `subscriptions` row."""
    u = _make_user(db_session, phone="+91NONE")
    _attach_nowlez(db_session, u, tier=None)  # never subscribed
    db_session.commit()

    summary = run_cutover(db_session, now=_NOW)

    assert summary["subscriptions_inserted"] == 0
    assert db_session.query(Subscription).count() == 0


def test_backfill_skips_legacy_free_tier(db_session):
    """`tier='free'` doesn't satisfy the subscriptions CHECK constraint;
    those users are handled by step 2's trial reset, not the subscription
    backfill."""
    u = _make_user(db_session, phone="+91FREE")
    _attach_nowlez(
        db_session, u,
        tier="free",
        # Pathological case: legacy free user with a razorpay_customer_id
        # set (e.g. they signed up for an upgrade flow that never
        # completed). Backfill must still skip them.
        razorpay_customer_id="cust_free_1",
        razorpay_subscription_id="sub_free_1",
    )
    db_session.commit()

    summary = run_cutover(db_session, now=_NOW)

    assert summary["subscriptions_inserted"] == 0
    assert db_session.query(Subscription).count() == 0


# ---------------------------------------------------------------------------
# Step 2 — free tier reset to trial
# ---------------------------------------------------------------------------


def test_free_users_reset_to_trial(db_session):
    u = _make_user(db_session, phone="+91FREE2")
    _attach_nowlez(db_session, u, tier="free", name="Alice")
    db_session.commit()

    summary = run_cutover(db_session, now=_NOW)

    assert summary["free_users_reset"] == 1
    nowlez_row = db_session.query(UserNowlez).filter_by(user_id=u.id).one()
    assert nowlez_row.tier is None
    assert nowlez_row.trial_started_at == _NOW
    assert nowlez_row.trial_ends_at == _NOW + timedelta(
        days=NOWLEZ_TRIAL_DURATION_DAYS
    )


def test_paid_tier_not_reset(db_session):
    u = _make_user(db_session, phone="+91PAID")
    _attach_nowlez(db_session, u, tier="advocate", name="Pat")
    db_session.commit()

    summary = run_cutover(db_session, now=_NOW)

    assert summary["free_users_reset"] == 0
    nowlez_row = db_session.query(UserNowlez).filter_by(user_id=u.id).one()
    assert nowlez_row.tier == "advocate"
    assert nowlez_row.trial_started_at is None


# ---------------------------------------------------------------------------
# Step 3 — Munshi anniversary verification
# ---------------------------------------------------------------------------


def test_run_cutover_succeeds_when_all_munshi_users_have_anniversary(db_session):
    u = _make_user(db_session, phone="+91MUNSHI")
    _attach_munshi(db_session, u, anniversary=date(2025, 5, 1))
    db_session.commit()

    summary = run_cutover(db_session, now=_NOW)
    assert summary["munshi_missing_anniversary"] == 0


def test_run_cutover_raises_when_munshi_anniversary_missing(db_session):
    u = _make_user(db_session, phone="+91NOANN")
    _attach_munshi(db_session, u, anniversary=None)
    db_session.commit()

    with pytest.raises(RuntimeError, match="billing_anniversary_date is NULL"):
        run_cutover(db_session, now=_NOW)


# ---------------------------------------------------------------------------
# Idempotency (Task 17.1.3 — explicit acceptance)
# ---------------------------------------------------------------------------


def test_cutover_is_idempotent_second_run_is_noop(db_session):
    """Run the full cutover twice. Second run inserts 0 subscriptions
    and resets 0 free users; final DB state is identical to a single run."""
    # Mixed cohort: one paying user, one free user, one Munshi user.
    payer = _make_user(db_session, phone="+91PAY3")
    _attach_nowlez(
        db_session, payer,
        tier="chambers",
        razorpay_customer_id="cust_idem",
        razorpay_subscription_id="sub_idem",
    )
    freebie = _make_user(db_session, phone="+91FRE3")
    _attach_nowlez(db_session, freebie, tier="free", name="Free")
    munshi = _make_user(db_session, phone="+91MNS3")
    _attach_munshi(db_session, munshi, anniversary=date(2025, 6, 1))
    db_session.commit()

    first = run_cutover(db_session, now=_NOW)
    assert first["subscriptions_inserted"] == 1
    assert first["free_users_reset"] == 1
    assert first["munshi_missing_anniversary"] == 0

    # Capture state for diffing.
    subs_after_first = db_session.query(Subscription).count()
    freebie_after_first = db_session.query(UserNowlez).filter_by(
        user_id=freebie.id,
    ).one()
    trial_ends_first = freebie_after_first.trial_ends_at

    # Second run — should be a no-op.
    second_now = _NOW + timedelta(days=1)  # different clock to prove idempotence
    second = run_cutover(db_session, now=second_now)
    assert second["subscriptions_inserted"] == 0
    assert second["free_users_reset"] == 0
    assert second["munshi_missing_anniversary"] == 0

    # Subscriptions row count unchanged.
    assert db_session.query(Subscription).count() == subs_after_first

    # Freebie trial window NOT extended — second run must not re-roll
    # the trial (which would let users farm endless trials via re-runs).
    freebie_after_second = db_session.query(UserNowlez).filter_by(
        user_id=freebie.id,
    ).one()
    assert freebie_after_second.trial_ends_at == trial_ends_first


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


def test_dry_run_reports_counts_without_mutating(db_session):
    payer = _make_user(db_session, phone="+91PAY4")
    _attach_nowlez(
        db_session, payer,
        tier="counsel",
        razorpay_customer_id="cust_dry",
        razorpay_subscription_id="sub_dry",
    )
    freebie = _make_user(db_session, phone="+91FRE4")
    _attach_nowlez(db_session, freebie, tier="free", name="Free2")
    db_session.commit()

    summary = run_cutover(db_session, now=_NOW, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["subscriptions_inserted"] == 1
    assert summary["free_users_reset"] == 1

    # No mutations.
    assert db_session.query(Subscription).count() == 0
    freebie_row = db_session.query(UserNowlez).filter_by(
        user_id=freebie.id,
    ).one()
    assert freebie_row.tier == "free"
    assert freebie_row.trial_started_at is None


# ---------------------------------------------------------------------------
# E-2 — verify step runs as a pre-flight (no writes when it fails)
# ---------------------------------------------------------------------------


def test_verify_runs_first_and_aborts_before_any_writes(db_session, monkeypatch):
    """E-2 fix: ``_verify_munshi_anniversary`` must execute BEFORE the
    backfill and free-tier reset. When it reports missing rows, the
    orchestrator must raise immediately and never call the two write
    steps. Seed enough data that both write steps would run if reached,
    so a regression (writes happening despite the verify failure) is
    observable both in mock-call count and in the DB state.
    """
    # Seed a paying user (backfill candidate) and a free user (reset
    # candidate) so both write steps would have non-zero work to do if
    # they were reached.
    payer = _make_user(db_session, phone="+91PRE1")
    _attach_nowlez(
        db_session, payer,
        tier="counsel",
        razorpay_customer_id="cust_pre1",
        razorpay_subscription_id="sub_pre1",
    )
    freebie = _make_user(db_session, phone="+91PRE2")
    _attach_nowlez(db_session, freebie, tier="free", name="PreFree")
    db_session.commit()

    backfill_calls: list[bool] = []
    reset_calls: list[bool] = []

    def _spy_backfill(session, *, now, dry_run):
        backfill_calls.append(True)
        return 0

    def _spy_reset(session, *, now, dry_run):
        reset_calls.append(True)
        return 0

    def _fake_verify(session):
        return 3  # >0 → pre-flight failure

    monkeypatch.setattr(_cutover_mod, "_backfill_subscriptions", _spy_backfill)
    monkeypatch.setattr(_cutover_mod, "_reset_free_tier_to_trial", _spy_reset)
    monkeypatch.setattr(_cutover_mod, "_verify_munshi_anniversary", _fake_verify)

    with pytest.raises(CutoverPreflightFailed, match="billing_anniversary_date"):
        run_cutover(db_session, now=_NOW)

    # The write steps must not have been called.
    assert backfill_calls == [], (
        "backfill ran despite pre-flight failure — E-2 regression"
    )
    assert reset_calls == [], (
        "free-tier reset ran despite pre-flight failure — E-2 regression"
    )

    # And no DB rows were mutated (defense in depth — proves the
    # mock-spy assertions aren't masking a direct SQL write).
    assert db_session.query(Subscription).count() == 0
    freebie_row = db_session.query(UserNowlez).filter_by(
        user_id=freebie.id,
    ).one()
    assert freebie_row.tier == "free"
    assert freebie_row.trial_started_at is None


def test_cutover_preflight_failed_is_runtimeerror_subclass():
    """Existing callers catch ``RuntimeError`` (see the pre-E-2 test
    above). The new ``CutoverPreflightFailed`` must remain catchable by
    that broader handler — keep it as a RuntimeError subclass."""
    assert issubclass(CutoverPreflightFailed, RuntimeError)
