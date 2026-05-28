from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# SQLite-compatibility variants: Postgres types with generic fallbacks for sqlite.
# Lets consumers (e.g. Munshi tests) use in-memory SQLite for Base.metadata.create_all().
UUIDType = UUID(as_uuid=True).with_variant(String(36), "sqlite")
INETType = INET().with_variant(String(45), "sqlite")
JSONBType = JSONB().with_variant(JSON(), "sqlite")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
        # NOTE: server_default=text("gen_random_uuid()") is omitted here because
        # SQLite errors on the literal at CREATE TABLE time. Prod schema is
        # preserved via the Alembic baseline migration (op.create_table sets
        # server_default explicitly). Python-side default=uuid.uuid4 covers ORM
        # INSERTs on both Postgres and SQLite.
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # Python attribute is `metadata_` because DeclarativeBase reserves `metadata`.
    # SQL column is the unprefixed `metadata`.
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONBType,
        nullable=False,
        default=dict,
        # server_default omitted: Postgres `'{}'::jsonb` cast syntax fails on
        # SQLite. Prod schema preserves server_default via Alembic baseline.
        # Python-side default=dict covers ORM INSERTs on both dialects.
    )
    ip_address: Mapped[str | None] = mapped_column(INETType, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sub-project G: forensic id of the migrated SQLite audit_logs row (the
    # consolidated backfill's idempotency key). NULL for natively-created events.
    legacy_sqlite_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "source IN ('munshi', 'nowlez', 'identity', 'system')",
            name="audit_source_check",
        ),
        Index("audit_log_created_at_idx", text("created_at DESC")),
        Index(
            "audit_log_user_id_idx",
            "user_id",
            text("created_at DESC"),
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index("audit_log_event_type_idx", "event_type", text("created_at DESC")),
    )
