"""Sub-project B: WhatsApp delivery log + idempotency + Nowlez consent columns.

Revision ID: 20260515_b_whatsapp
Revises: 20260601_e_billing
Create Date: 2026-05-15

Adds:
  - users_nowlez.whatsapp_events_enabled / whatsapp_reminders_enabled (opt-out
    defaults TRUE)
  - whatsapp_delivery_log table (per-send tracking with FKs to users, cases,
    case_orders) + supporting indexes + brand/status CHECK constraints
  - message_log table (inbound idempotency, keyed by Meta wamid)

NOTE: Several index expressions and partial-index WHERE clauses use Postgres
syntax (`enqueued_at DESC`, partial WHERE). The SQLite migration test path
either skips the partial WHERE (becoming a full index) or skips the
expression-index path entirely — production runs against Postgres so the
full intent is preserved there.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260515_b_whatsapp"
down_revision = "20260601_e_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Per-user WhatsApp consent flags on users_nowlez.
    op.add_column(
        "users_nowlez",
        sa.Column(
            "whatsapp_events_enabled", sa.Boolean(),
            nullable=False, server_default=sa.text("TRUE"),
        ),
    )
    op.add_column(
        "users_nowlez",
        sa.Column(
            "whatsapp_reminders_enabled", sa.Boolean(),
            nullable=False, server_default=sa.text("TRUE"),
        ),
    )

    # 2. Per-send tracking table.
    op.create_table(
        "whatsapp_delivery_log",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()") if dialect == "postgresql" else None,
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_name", sa.Text(), nullable=False),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("meta_message_id", sa.Text()),
        sa.Column("rq_job_id", sa.Text()),
        sa.Column(
            "delivery_status", sa.Text(),
            nullable=False, server_default="pending",
        ),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("related_case_id", postgresql.UUID(as_uuid=True)),
        sa.Column("related_order_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "enqueued_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["related_case_id"], ["cases.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["related_order_id"], ["case_orders.id"], ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "brand IN ('munshi', 'nowlez')",
            name="whatsapp_delivery_log_brand_check",
        ),
        sa.CheckConstraint(
            "delivery_status IN ('pending', 'sent', 'delivered', 'read', 'failed')",
            name="whatsapp_delivery_log_status_check",
        ),
    )
    if dialect == "postgresql":
        op.create_index(
            "whatsapp_delivery_log_user_id_idx", "whatsapp_delivery_log",
            ["user_id", sa.text("enqueued_at DESC")],
        )
        op.execute(
            "CREATE INDEX whatsapp_delivery_log_status_idx "
            "ON whatsapp_delivery_log(delivery_status) "
            "WHERE delivery_status IN ('pending', 'failed')"
        )
    else:
        # SQLite: skip the DESC + partial-index niceties — keep a plain index
        # for the unit-test path so test schemas remain queryable.
        op.create_index(
            "whatsapp_delivery_log_user_id_idx", "whatsapp_delivery_log",
            ["user_id", "enqueued_at"],
        )
        op.create_index(
            "whatsapp_delivery_log_status_idx", "whatsapp_delivery_log",
            ["delivery_status"],
        )
    op.create_index(
        "whatsapp_delivery_log_meta_msg_idx", "whatsapp_delivery_log",
        ["meta_message_id"],
    )

    # 3. message_log — shared idempotency table. May already exist from Munshi
    # legacy migrations; CREATE TABLE IF NOT EXISTS keeps both deploy paths
    # idempotent. Carries Munshi's legacy columns (user_id, processed_at,
    # handler_name, error) so the relocated MessageLog model is shape-compat
    # with Munshi's existing app.py / idempotency.py.
    if dialect == "postgresql":
        op.execute(
            "CREATE TABLE IF NOT EXISTS message_log ("
            "  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            "  meta_message_id TEXT NOT NULL,"
            "  user_phone TEXT,"
            "  user_id UUID REFERENCES users(id) ON DELETE SET NULL,"
            "  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "  processed_at TIMESTAMPTZ,"
            "  handler_name TEXT,"
            "  error TEXT,"
            "  CONSTRAINT message_log_meta_id_unique UNIQUE (meta_message_id)"
            ")"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS message_log_user_phone_idx "
            "ON message_log(user_phone)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS message_log_user_id_idx "
            "ON message_log(user_id)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS message_log_received_idx "
            "ON message_log(received_at)"
        )
    else:
        # SQLite create-if-not-exists doesn't support every Postgres feature,
        # but the test path uses metadata.create_all() to materialize the
        # model directly so this migration branch is intentionally a no-op.
        pass


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.drop_index(
        "whatsapp_delivery_log_meta_msg_idx", table_name="whatsapp_delivery_log",
    )
    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS whatsapp_delivery_log_status_idx")
    else:
        op.drop_index(
            "whatsapp_delivery_log_status_idx", table_name="whatsapp_delivery_log",
        )
    op.drop_index(
        "whatsapp_delivery_log_user_id_idx", table_name="whatsapp_delivery_log",
    )
    op.drop_table("whatsapp_delivery_log")
    op.drop_column("users_nowlez", "whatsapp_reminders_enabled")
    op.drop_column("users_nowlez", "whatsapp_events_enabled")
    # message_log is intentionally NOT dropped: it predates this migration
    # in Munshi's deploy and may still hold legacy rows the rest of the
    # stack hasn't migrated off yet.
