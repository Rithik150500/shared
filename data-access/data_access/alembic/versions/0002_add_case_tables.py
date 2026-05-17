"""add cases, case_orders, case_orders_nowlez

Revision ID: 0002_add_case_tables
Revises: 20260515_0001
Create Date: 2026-05-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_add_case_tables"
down_revision = "20260515_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cnr", sa.String(length=16), nullable=False),
        sa.Column("case_number", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("portal", sa.Text(), nullable=False),
        sa.Column("filing_year", sa.Integer()),
        sa.Column("court", sa.Text()),
        sa.Column("judge", sa.Text()),
        sa.Column("stage", sa.Text()),
        sa.Column("case_status", sa.Text()),
        sa.Column("next_hearing_date", sa.DateTime(timezone=True)),
        sa.Column("refresh_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True)),
        sa.Column("last_change_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text()),
        sa.Column("client_id", sa.Text()),
        sa.Column("parties", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("acts", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("history", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("fir", postgresql.JSONB()),
        sa.Column("objections", postgresql.JSONB()),
        sa.Column("category", postgresql.JSONB()),
        sa.Column("raw_response", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "cnr", name="cases_user_cnr_unique"),
        sa.CheckConstraint("portal IN ('district', 'highcourt')", name="cases_portal_check"),
    )
    op.create_index("cases_user_id_idx", "cases", ["user_id"])
    op.create_index(
        "cases_next_hearing_date_idx", "cases", ["next_hearing_date"],
        postgresql_where=sa.text("next_hearing_date IS NOT NULL"),
    )
    op.create_index(
        "cases_last_change_at_idx", "cases", [sa.text("last_change_at DESC")],
        postgresql_where=sa.text("last_change_at IS NOT NULL"),
    )
    op.create_index(
        "cases_refresh_queue_idx", "cases",
        ["refresh_enabled", sa.text("last_refreshed_at NULLS FIRST")],
        postgresql_where=sa.text("refresh_enabled IS TRUE"),
    )
    op.create_index(
        "cases_client_id_idx", "cases", ["client_id"],
        postgresql_where=sa.text("client_id IS NOT NULL"),
    )
    op.create_index("cases_cnr_idx", "cases", ["cnr"])

    op.create_table(
        "case_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("order_date", sa.Date(), nullable=False),
        sa.Column("descriptive_name", sa.Text()),
        sa.Column("order_url", sa.Text()),
        sa.Column("url_fetched_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "order_id", name="case_orders_case_order_unique"),
    )
    op.create_index("case_orders_case_id_idx", "case_orders", ["case_id"])
    op.create_index(
        "case_orders_order_date_idx", "case_orders",
        ["case_id", sa.text("order_date DESC")],
    )

    op.create_table(
        "case_orders_nowlez",
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_path", sa.Text()),
        sa.Column("file_storage", sa.Text()),
        sa.Column("page_count", sa.Integer()),
        sa.Column("file_size_bytes", sa.BigInteger()),
        sa.Column("preprocessed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("preprocessed_markdown_path", sa.Text()),
        sa.Column("preprocessed_at", sa.DateTime(timezone=True)),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_retry_at", sa.DateTime(timezone=True)),
        sa.Column("permanently_failed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("permanent_failure_reason", sa.Text()),
        sa.Column("uploaded_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["order_id"], ["case_orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("order_id"),
        sa.CheckConstraint(
            "file_storage IS NULL OR file_storage IN ('r2', 'local')",
            name="case_orders_nowlez_storage_check",
        ),
    )
    op.create_index(
        "case_orders_nowlez_preprocess_queue_idx", "case_orders_nowlez",
        ["preprocessed", "retry_count", sa.text("last_retry_at NULLS FIRST")],
        postgresql_where=sa.text("preprocessed IS FALSE AND permanently_failed IS FALSE"),
    )


def downgrade() -> None:
    op.drop_index("case_orders_nowlez_preprocess_queue_idx", table_name="case_orders_nowlez")
    op.drop_table("case_orders_nowlez")
    op.drop_index("case_orders_order_date_idx", table_name="case_orders")
    op.drop_index("case_orders_case_id_idx", table_name="case_orders")
    op.drop_table("case_orders")
    op.drop_index("cases_cnr_idx", table_name="cases")
    op.drop_index("cases_client_id_idx", table_name="cases")
    op.drop_index("cases_refresh_queue_idx", table_name="cases")
    op.drop_index("cases_last_change_at_idx", table_name="cases")
    op.drop_index("cases_next_hearing_date_idx", table_name="cases")
    op.drop_index("cases_user_id_idx", table_name="cases")
    op.drop_table("cases")
