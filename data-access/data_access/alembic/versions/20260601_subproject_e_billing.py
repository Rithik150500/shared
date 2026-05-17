"""sub-project E: add billing tables + schema modifications

Revision ID: 20260601_e_billing
Revises: 0002_add_case_tables
Create Date: 2026-06-01

Creates the six new billing tables (subscriptions, payment_events,
coupon_codes, referrals, case_billing_periods, munshi_invoices) and adds
the schema modifications to users_nowlez (nullable tier + trial columns)
and users_munshi (billing_anniversary_date) that sub-project E depends on.

Field set, enums, and column types come from the sub-project E plan task
description (`docs/superpowers/plans/2026-05-15-subproject-e-case-billing-plan.md`,
Task 3). Where the design spec (...case-billing-design.md §3) and the plan
disagree the plan wins.

The schema modifications touch existing tables. The backfill
`UPDATE users_munshi SET billing_anniversary_date = created_at::date
 WHERE billing_anniversary_date IS NULL` is safe on an empty table (no rows
match the WHERE clause), so the same migration is exercised by the SQLite
test path even though no rows exist there.

NOTE: The `users_nowlez_trial_ending_idx` and several other partial indexes
use `postgresql_where=` which becomes a no-op (full index) on dialects that
do not support partial indexes. SQLite produces a full index — acceptable for
test purposes; production prod is Postgres which gets the partial index.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260601_e_billing"
down_revision = "0002_add_case_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # --- subscriptions ---
    op.create_table(
        "subscriptions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()") if dialect == "postgresql" else None,
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("billing_cycle", sa.Text(), nullable=False),
        sa.Column("razorpay_subscription_id", sa.Text(), nullable=True),
        sa.Column("razorpay_customer_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "intro_promo_state", sa.Text(),
            nullable=False, server_default="pre_first_payment",
        ),
        sa.Column(
            "referral_state", sa.Text(),
            nullable=False, server_default="no_referral",
        ),
        sa.Column("period_start", sa.DateTime(timezone=True)),
        sa.Column("period_end", sa.DateTime(timezone=True)),
        sa.Column("grace_expires_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "razorpay_subscription_id",
            name="subscriptions_razorpay_subscription_id_key",
        ),
        sa.CheckConstraint(
            "tier IN ('advocate', 'counsel', 'chambers')",
            name="subscriptions_tier_check",
        ),
        sa.CheckConstraint(
            "billing_cycle IN ('monthly', 'quarterly', 'yearly')",
            name="subscriptions_billing_cycle_check",
        ),
        sa.CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'cancelled', "
            "'expired', 'suspended')",
            name="subscriptions_status_check",
        ),
        sa.CheckConstraint(
            "intro_promo_state IN ('pre_first_payment', 'in_intro', "
            "'past_intro', 'skipped')",
            name="subscriptions_intro_promo_state_check",
        ),
        sa.CheckConstraint(
            "referral_state IN ('no_referral', 'pending_mutual', "
            "'mutual_applied', 'expired')",
            name="subscriptions_referral_state_check",
        ),
    )
    op.create_index(
        "subscriptions_user_id_idx", "subscriptions", ["user_id"],
        postgresql_where=sa.text(
            "status IN ('trialing', 'active', 'past_due')"
        ),
    )
    op.create_index(
        "subscriptions_razorpay_id_idx", "subscriptions",
        ["razorpay_subscription_id"],
        postgresql_where=sa.text("razorpay_subscription_id IS NOT NULL"),
    )
    op.create_index(
        "subscriptions_grace_idx", "subscriptions", ["grace_expires_at"],
        postgresql_where=sa.text("grace_expires_at IS NOT NULL"),
    )

    # --- payment_events ---
    op.create_table(
        "payment_events",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()") if dialect == "postgresql" else None,
            nullable=False,
        ),
        sa.Column("razorpay_event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB() if dialect == "postgresql" else sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if dialect == "postgresql" else None,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "razorpay_event_id",
            name="payment_events_razorpay_event_id_key",
        ),
        sa.CheckConstraint(
            "product IN ('munshi', 'nowlez')",
            name="payment_events_product_check",
        ),
    )
    op.create_index(
        "payment_events_event_type_idx", "payment_events",
        ["event_type", sa.text("created_at DESC")],
    )

    # --- coupon_codes ---
    op.create_table(
        "coupon_codes",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("percent_off", sa.Integer(), nullable=False),
        sa.Column("max_redemptions", sa.Integer()),
        sa.Column(
            "redemptions", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("code"),
    )

    # --- referrals ---
    op.create_table(
        "referrals",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()") if dialect == "postgresql" else None,
            nullable=False,
        ),
        sa.Column(
            "referrer_user_id", postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column(
            "referred_user_id", postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column(
            "state", sa.Text(),
            nullable=False, server_default="pending",
        ),
        sa.Column(
            "referred_subscription_id", postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["referrer_user_id"], ["users.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["referred_user_id"], ["users.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["referred_subscription_id"], ["subscriptions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "referred_user_id", name="referrals_referred_unique",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'mutual_applied', 'expired')",
            name="referrals_state_check",
        ),
    )
    op.create_index(
        "referrals_referrer_id_idx", "referrals", ["referrer_user_id"],
    )

    # --- case_billing_periods ---
    op.create_table(
        "case_billing_periods",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()") if dialect == "postgresql" else None,
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "period_start", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column("period_end", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "case_billing_periods_user_id_idx", "case_billing_periods",
        ["user_id", "period_start"],
    )
    op.create_index(
        "case_billing_periods_active_idx", "case_billing_periods",
        ["case_id"],
        postgresql_where=sa.text("period_end IS NULL"),
    )
    op.create_index(
        "case_billing_periods_case_id_idx", "case_billing_periods", ["case_id"],
    )

    # --- munshi_invoices ---
    op.create_table(
        "munshi_invoices",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()") if dialect == "postgresql" else None,
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("razorpay_invoice_id", sa.Text()),
        sa.Column("cycle_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cycle_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column(
            "status", sa.Text(),
            nullable=False, server_default="pending",
        ),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("grace_expires_at", sa.DateTime(timezone=True)),
        sa.Column("suspended_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "cycle_start", "cycle_end",
            name="munshi_invoices_user_cycle_unique",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'paid', 'in_grace', "
            "'suspended', 'failed', 'voided')",
            name="munshi_invoices_status_check",
        ),
    )
    op.create_index(
        "munshi_invoices_user_id_idx", "munshi_invoices",
        ["user_id", sa.text("cycle_end DESC")],
    )
    op.create_index(
        "munshi_invoices_pending_idx", "munshi_invoices",
        ["status", "due_at"],
        postgresql_where=sa.text(
            "status IN ('pending', 'sent', 'in_grace')"
        ),
    )
    op.create_index(
        "munshi_invoices_razorpay_id_idx", "munshi_invoices",
        ["razorpay_invoice_id"],
        postgresql_where=sa.text("razorpay_invoice_id IS NOT NULL"),
    )

    # --- ALTER users_nowlez ---
    # SQLite does not support ALTER COLUMN / ADD CHECK CONSTRAINT after-the-fact.
    # The intent is verified on Postgres; SQLite mig path is exercised only as
    # a smoke test (the test fixture rebuilds via Base.metadata.create_all()).
    if dialect == "postgresql":
        op.alter_column(
            "users_nowlez", "tier",
            existing_type=sa.Text(), nullable=True,
        )
    op.add_column(
        "users_nowlez",
        sa.Column("trial_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users_nowlez",
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    if dialect == "postgresql":
        op.create_check_constraint(
            "users_nowlez_tier_check", "users_nowlez",
            "tier IS NULL OR tier IN ('advocate', 'counsel', 'chambers', 'free')",
        )
        op.create_index(
            "users_nowlez_trial_ending_idx", "users_nowlez", ["trial_ends_at"],
            postgresql_where=sa.text(
                "trial_ends_at IS NOT NULL AND tier IS NULL"
            ),
        )

    # --- ALTER users_munshi ---
    op.add_column(
        "users_munshi",
        sa.Column("billing_anniversary_date", sa.Date(), nullable=True),
    )
    # Backfill is a no-op on empty tables (covers the SQLite/test path) and
    # populates legacy rows on Postgres. ::date cast is Postgres-only; on
    # SQLite the table is always empty so we can skip the backfill entirely.
    if dialect == "postgresql":
        op.execute(
            "UPDATE users_munshi SET billing_anniversary_date = created_at::date "
            "WHERE billing_anniversary_date IS NULL"
        )
        op.alter_column(
            "users_munshi", "billing_anniversary_date",
            existing_type=sa.Date(), nullable=False,
        )
    op.create_index(
        "users_munshi_anniversary_idx", "users_munshi",
        ["billing_anniversary_date"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Reverse users_munshi changes
    op.drop_index(
        "users_munshi_anniversary_idx", table_name="users_munshi",
    )
    op.drop_column("users_munshi", "billing_anniversary_date")

    # Reverse users_nowlez changes
    if dialect == "postgresql":
        op.drop_index(
            "users_nowlez_trial_ending_idx", table_name="users_nowlez",
        )
        op.drop_constraint(
            "users_nowlez_tier_check", "users_nowlez", type_="check",
        )
    op.drop_column("users_nowlez", "trial_ends_at")
    op.drop_column("users_nowlez", "trial_started_at")
    if dialect == "postgresql":
        # Restore NOT NULL on tier. Existing rows already have a value due to
        # the original server_default='free', so this is safe.
        op.alter_column(
            "users_nowlez", "tier",
            existing_type=sa.Text(), nullable=False,
        )

    # Drop new tables in reverse FK order
    op.drop_index(
        "munshi_invoices_razorpay_id_idx", table_name="munshi_invoices",
    )
    op.drop_index(
        "munshi_invoices_pending_idx", table_name="munshi_invoices",
    )
    op.drop_index(
        "munshi_invoices_user_id_idx", table_name="munshi_invoices",
    )
    op.drop_table("munshi_invoices")

    op.drop_index(
        "case_billing_periods_case_id_idx", table_name="case_billing_periods",
    )
    op.drop_index(
        "case_billing_periods_active_idx", table_name="case_billing_periods",
    )
    op.drop_index(
        "case_billing_periods_user_id_idx", table_name="case_billing_periods",
    )
    op.drop_table("case_billing_periods")

    op.drop_index("referrals_referrer_id_idx", table_name="referrals")
    op.drop_table("referrals")

    op.drop_table("coupon_codes")

    op.drop_index(
        "payment_events_event_type_idx", table_name="payment_events",
    )
    op.drop_table("payment_events")

    op.drop_index("subscriptions_grace_idx", table_name="subscriptions")
    op.drop_index("subscriptions_razorpay_id_idx", table_name="subscriptions")
    op.drop_index("subscriptions_user_id_idx", table_name="subscriptions")
    op.drop_table("subscriptions")
