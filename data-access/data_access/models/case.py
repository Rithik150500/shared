"""Unified case-tracking models shared by Munshi and Nowlez."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base


# UUID type that survives the SQLite round-trip. Sub-project D's plain
# `UUID(...).with_variant(String(36), "sqlite")` only adjusts DDL — on read,
# SQLite hands back a str, and identity-map lookups against an in-memory
# UUID() instance miss (because str != UUID). This TypeDecorator coerces
# both directions so DAO callers always see uuid.UUID regardless of dialect.
class _UUIDType(TypeDecorator):
    impl = UUID(as_uuid=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(36))
        return dialect.type_descriptor(UUID(as_uuid=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite":
            return str(value) if isinstance(value, uuid.UUID) else value
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite" and isinstance(value, str):
            return uuid.UUID(value)
        return value


UUIDType = _UUIDType()
JSONBType = JSONB().with_variant(JSON(), "sqlite")


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
        # NOTE: server_default=text("gen_random_uuid()") is omitted here because
        # SQLite errors on the literal at CREATE TABLE time. Prod schema is
        # preserved via the Alembic migration (op.create_table sets
        # server_default explicitly). Python-side default=uuid.uuid4 covers ORM
        # INSERTs on both Postgres and SQLite.
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    cnr: Mapped[str] = mapped_column(String(16), nullable=False)
    case_number: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    portal: Mapped[str] = mapped_column(Text, nullable=False)
    filing_year: Mapped[int | None] = mapped_column(Integer)

    court: Mapped[str | None] = mapped_column(Text)
    judge: Mapped[str | None] = mapped_column(Text)

    stage: Mapped[str | None] = mapped_column(Text)
    case_status: Mapped[str | None] = mapped_column(Text)
    next_hearing_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    refresh_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # B.5b: timestamp of the first NDOH email dispatched for this case. NULL
    # means "no first-NDOH email has been sent yet" — set by the Nowlez E2
    # hook on first-send, never cleared. Replaces the SQLite-side flag the
    # Nowlez bot used to gate one-shot E2 sends across restarts.
    first_ndoh_email_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(Text)
    client_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("clients.id", ondelete="SET NULL"), nullable=True,
    )

    # JSONB columns: Python-side defaults so SQLite INSERTs work without the
    # Postgres `'[]'::jsonb` server_default literal.
    parties: Mapped[list[Any]] = mapped_column(JSONBType, nullable=False, default=list)
    acts: Mapped[list[Any]] = mapped_column(JSONBType, nullable=False, default=list)
    history: Mapped[list[Any]] = mapped_column(JSONBType, nullable=False, default=list)
    fir: Mapped[dict[str, Any] | None] = mapped_column(JSONBType)
    objections: Mapped[dict[str, Any] | None] = mapped_column(JSONBType)
    category: Mapped[dict[str, Any] | None] = mapped_column(JSONBType)

    raw_response: Mapped[dict[str, Any]] = mapped_column(
        JSONBType, nullable=False, default=dict
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    orders: Mapped[list["CaseOrder"]] = relationship(
        "CaseOrder", back_populates="case", cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("user_id", "cnr", name="cases_user_cnr_unique"),
        CheckConstraint("portal IN ('district', 'highcourt')", name="cases_portal_check"),
        Index("cases_user_id_idx", "user_id"),
        Index(
            "cases_next_hearing_date_idx", "next_hearing_date",
            postgresql_where=text("next_hearing_date IS NOT NULL"),
        ),
        Index(
            "cases_last_change_at_idx", text("last_change_at DESC"),
            postgresql_where=text("last_change_at IS NOT NULL"),
        ),
        Index(
            "cases_refresh_queue_idx", "refresh_enabled", "last_refreshed_at",
            postgresql_where=text("refresh_enabled IS TRUE"),
        ),
        Index(
            "cases_client_id_idx", "client_id",
            postgresql_where=text("client_id IS NOT NULL"),
        ),
        Index("cases_cnr_idx", "cnr"),
    )


class CaseOrder(Base):
    __tablename__ = "case_orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    descriptive_name: Mapped[str | None] = mapped_column(Text)
    order_url: Mapped[str | None] = mapped_column(Text)
    url_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    case: Mapped["Case"] = relationship("Case", back_populates="orders")
    nowlez_ext: Mapped["CaseOrderNowlez | None"] = relationship(
        "CaseOrderNowlez", back_populates="order", cascade="all, delete-orphan", uselist=False,
    )

    __table_args__ = (
        UniqueConstraint("case_id", "order_id", name="case_orders_case_order_unique"),
        Index("case_orders_case_id_idx", "case_id"),
        Index("case_orders_order_date_idx", "case_id", text("order_date DESC")),
    )


class CaseOrderNowlez(Base):
    __tablename__ = "case_orders_nowlez"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("case_orders.id", ondelete="CASCADE"),
        primary_key=True,
    )
    file_path: Mapped[str | None] = mapped_column(Text)
    file_storage: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    preprocessed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    preprocessed_markdown_path: Mapped[str | None] = mapped_column(Text)
    preprocessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    permanently_failed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    permanent_failure_reason: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # B.5b: timestamp of the E3 "order user-notified" WhatsApp message. NULL
    # means "user has not yet been told about this order". The Nowlez hook
    # picks unnotified orders via `WHERE user_notified_at IS NULL` so the
    # default NULL on new rows correctly enqueues each order exactly once.
    user_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    order: Mapped["CaseOrder"] = relationship("CaseOrder", back_populates="nowlez_ext")

    __table_args__ = (
        CheckConstraint(
            "file_storage IS NULL OR file_storage IN ('r2', 'local')",
            name="case_orders_nowlez_storage_check",
        ),
        Index(
            "case_orders_nowlez_preprocess_queue_idx",
            "preprocessed", "retry_count", "last_retry_at",
            postgresql_where=text(
                "preprocessed IS FALSE AND permanently_failed IS FALSE"
            ),
        ),
    )
