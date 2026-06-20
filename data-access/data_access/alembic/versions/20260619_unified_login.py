"""unified-auth: login_requests + email_otp_codes + users_nowlez.email_verified

Revision ID: 20260619_unified_login
Revises: 20260616_broadcast_tables
Create Date: 2026-06-19

Phase 1 of the unified WhatsApp-OTP / email-OTP auth work. Creates:

login_requests
  DB-authoritative single-use nonce ledger for the web<->bot login bridge.
  status flows pending->confirmed->consumed (or ->expired); every transition
  is an atomic conditional UPDATE in login_request_dao. CHECKs on
  direction/status/brand. Partial expiry sweep index covers BOTH pending and
  confirmed (confirmed-but-stale rows must also be swept).

  token_hash has unique=True on the column; the UNIQUE constraint (named
  login_requests_token_hash_key to match the Postgres default) is created
  inline — no separate non-unique index (that would be redundant write
  amplification and was deliberately omitted from the model).

email_otp_codes
  Email-channel OTP store (separate from otp_codes: email won't fit
  phone String(20) and the otp_codes channel CHECK is ('whatsapp','sms')).
  Mirrors otp_codes shape; argon2 code_hash; atomic mark_used in the DAO.

users_nowlez.email_verified
  D4 "verified email on account" boolean (default false), populated on the
  first successful email-OTP verify and on the reviewed Sub-A/Sub-G backfill.

down_revision chains onto 20260616_broadcast_tables, the single head per
``alembic heads`` on 2026-06-19. Revision id is 20 chars (<= VARCHAR(32)).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260619_unified_login"
down_revision = "20260616_broadcast_tables"
branch_labels = None
depends_on = None


# Copied verbatim from 20260616_broadcast_tables.py: native UUID on Postgres,
# String(36) on SQLite (unit-test path).
def _uuid_col(dialect_name: str) -> sa.types.TypeEngine:
    if dialect_name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(36)


def _inet_col(dialect_name: str) -> sa.types.TypeEngine:
    if dialect_name == "postgresql":
        return postgresql.INET()
    return sa.String(45)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    _uuid = _uuid_col(dialect)
    _inet = _inet_col(dialect)
    is_pg = dialect == "postgresql"
    id_server_default = sa.text("gen_random_uuid()") if is_pg else None

    # -----------------------------------------------------------------------
    # login_requests
    # -----------------------------------------------------------------------
    op.create_table(
        "login_requests",
        sa.Column("id", _uuid, primary_key=True, server_default=id_server_default),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column(
            "user_id",
            _uuid,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("poll_bind_hash", sa.Text(), nullable=True),
        sa.Column("ip_address", _inet, nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        # token_hash unique constraint: named to match the Postgres default
        # (column unique=True -> "<table>_<col>_key"). No separate non-unique
        # index — the UNIQUE constraint's implicit index serves equality lookups
        # without double write-amplification.
        sa.UniqueConstraint("token_hash", name="login_requests_token_hash_key"),
        sa.CheckConstraint(
            "direction IN ('web2bot', 'bot2web')",
            name="login_requests_direction_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'consumed', 'expired')",
            name="login_requests_status_check",
        ),
        sa.CheckConstraint(
            "brand IN ('munshi', 'nowlez')",
            name="login_requests_brand_check",
        ),
    )
    # Partial expiry-sweep index: covers pending AND confirmed rows (confirmed-
    # but-stale must also be swept). Postgres rejects volatile NOW() in index
    # predicates so we filter on status only.
    op.create_index(
        "login_requests_expires_at_idx",
        "login_requests",
        ["expires_at"],
        postgresql_where=sa.text("status IN ('pending', 'confirmed')"),
    )
    op.create_index(
        "login_requests_ip_rate_idx",
        "login_requests",
        ["ip_address", "created_at"],
    )

    # -----------------------------------------------------------------------
    # email_otp_codes
    # -----------------------------------------------------------------------
    op.create_table(
        "email_otp_codes",
        sa.Column("id", _uuid, primary_key=True, server_default=id_server_default),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column(
            "delivery_status", sa.Text(), nullable=False, server_default="pending"
        ),
        sa.Column("delivery_provider_id", sa.Text(), nullable=True),
        sa.Column(
            "attempts_remaining",
            sa.SmallInteger(),
            nullable=False,
            server_default="3",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", _inet, nullable=True),
        sa.CheckConstraint(
            "delivery_status IN ('pending', 'delivered', 'failed')",
            name="email_otp_delivery_status_check",
        ),
    )
    # Partial active-OTP lookup index (used_at IS NULL = not yet consumed).
    # Postgres rejects volatile NOW() in predicates; filter on used_at only.
    op.create_index(
        "email_otp_email_active_idx",
        "email_otp_codes",
        ["email", "created_at"],
        postgresql_where=sa.text("used_at IS NULL"),
    )
    op.create_index(
        "email_otp_email_rate_idx",
        "email_otp_codes",
        ["email", "created_at"],
    )
    op.create_index(
        "email_otp_expires_idx",
        "email_otp_codes",
        ["expires_at"],
        postgresql_where=sa.text("used_at IS NULL"),
    )

    # -----------------------------------------------------------------------
    # users_nowlez.email_verified (D4 signal)
    # -----------------------------------------------------------------------
    op.add_column(
        "users_nowlez",
        sa.Column(
            "email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users_nowlez", "email_verified")

    op.drop_index("email_otp_expires_idx", table_name="email_otp_codes")
    op.drop_index("email_otp_email_rate_idx", table_name="email_otp_codes")
    op.drop_index("email_otp_email_active_idx", table_name="email_otp_codes")
    op.drop_table("email_otp_codes")

    op.drop_index("login_requests_ip_rate_idx", table_name="login_requests")
    op.drop_index("login_requests_expires_at_idx", table_name="login_requests")
    op.drop_table("login_requests")
