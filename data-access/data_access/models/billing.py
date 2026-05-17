"""Sub-project E billing models — subscriptions, payment events, coupons,
referrals, case-billing periods, Munshi invoices.

Column / type choices follow the plan task description in
`docs/superpowers/plans/2026-05-15-subproject-e-case-billing-plan.md` (Task 3),
which deliberately narrows some of the spec's enums and column sets. Where the
plan and the design-spec disagree the plan wins (see plan §3 note).

SQLite-compat: same pattern as user.py / case.py — `with_variant(...)` for DDL,
Python-side `default=uuid.uuid4` so tests that build the schema with
`Base.metadata.create_all()` on in-memory SQLite work without the Postgres
`gen_random_uuid()` literal.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# SQLite-compat variants. See user.py / case.py for rationale.
UUIDType = UUID(as_uuid=True).with_variant(String(36), "sqlite")
JSONBType = JSONB().with_variant(JSON(), "sqlite")


class Subscription(Base):
    """Nowlez subscription state, including intro-promo and referral sub-states."""

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    billing_cycle: Mapped[str] = mapped_column(Text, nullable=False)
    razorpay_subscription_id: Mapped[str | None] = mapped_column(
        Text, unique=True, nullable=True,
    )
    razorpay_customer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    intro_promo_state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="pre_first_payment",
        server_default="pre_first_payment",
    )
    referral_state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="no_referral",
        server_default="no_referral",
    )
    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    grace_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    cancel_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "tier IN ('advocate', 'counsel', 'chambers')",
            name="subscriptions_tier_check",
        ),
        CheckConstraint(
            "billing_cycle IN ('monthly', 'quarterly', 'yearly')",
            name="subscriptions_billing_cycle_check",
        ),
        CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'cancelled', "
            "'expired', 'suspended')",
            name="subscriptions_status_check",
        ),
        CheckConstraint(
            "intro_promo_state IN ('pre_first_payment', 'in_intro', "
            "'past_intro', 'skipped')",
            name="subscriptions_intro_promo_state_check",
        ),
        CheckConstraint(
            "referral_state IN ('no_referral', 'pending_mutual', "
            "'mutual_applied', 'expired')",
            name="subscriptions_referral_state_check",
        ),
        Index(
            "subscriptions_user_id_idx",
            "user_id",
            postgresql_where=text(
                "status IN ('trialing', 'active', 'past_due')"
            ),
        ),
        Index(
            "subscriptions_razorpay_id_idx",
            "razorpay_subscription_id",
            postgresql_where=text("razorpay_subscription_id IS NOT NULL"),
        ),
        Index(
            "subscriptions_grace_idx",
            "grace_expires_at",
            postgresql_where=text("grace_expires_at IS NOT NULL"),
        ),
    )


class PaymentEvent(Base):
    """Razorpay webhook idempotency log."""

    __tablename__ = "payment_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
    )
    razorpay_event_id: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    product: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONBType, nullable=False, default=dict,
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "product IN ('munshi', 'nowlez')",
            name="payment_events_product_check",
        ),
        Index(
            "payment_events_event_type_idx",
            "event_type",
            text("created_at DESC"),
        ),
    )


class CouponCode(Base):
    """Promo coupon definitions (code is the PK per plan task description)."""

    __tablename__ = "coupon_codes"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    percent_off: Mapped[int] = mapped_column(Integer, nullable=False)
    max_redemptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redemptions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class Referral(Base):
    """Referrer-referree relationship; 1:1 on referred_user_id."""

    __tablename__ = "referrals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
    )
    referrer_user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    referred_user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending",
    )
    referred_subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("referred_user_id", name="referrals_referred_unique"),
        CheckConstraint(
            "state IN ('pending', 'mutual_applied', 'expired')",
            name="referrals_state_check",
        ),
        Index("referrals_referrer_id_idx", "referrer_user_id"),
    )


class CaseBillingPeriod(Base):
    """Per-case billable window. NULL period_end == still active."""

    __tablename__ = "case_billing_periods"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "case_billing_periods_user_id_idx", "user_id", "period_start",
        ),
        Index(
            "case_billing_periods_active_idx",
            "case_id",
            postgresql_where=text("period_end IS NULL"),
        ),
        Index("case_billing_periods_case_id_idx", "case_id"),
    )


class MunshiInvoice(Base):
    """Munshi postpaid invoice, one per user per cycle."""

    __tablename__ = "munshi_invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    razorpay_invoice_id: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    cycle_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    cycle_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending",
    )
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    grace_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    suspended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "cycle_start", "cycle_end",
            name="munshi_invoices_user_cycle_unique",
        ),
        CheckConstraint(
            "status IN ('pending', 'sent', 'paid', 'in_grace', "
            "'suspended', 'failed', 'voided')",
            name="munshi_invoices_status_check",
        ),
        Index(
            "munshi_invoices_user_id_idx",
            "user_id",
            text("cycle_end DESC"),
        ),
        Index(
            "munshi_invoices_pending_idx",
            "status", "due_at",
            postgresql_where=text(
                "status IN ('pending', 'sent', 'in_grace')"
            ),
        ),
        Index(
            "munshi_invoices_razorpay_id_idx",
            "razorpay_invoice_id",
            postgresql_where=text("razorpay_invoice_id IS NOT NULL"),
        ),
    )
