from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# Postgres UUID with a String(36) fallback for SQLite (tests). See models/user.py.
UUIDType = UUID(as_uuid=True).with_variant(String(36), "sqlite")


class Feedback(Base):
    """User feedback submission. Migrated from SQLite in Sub-project G
    (housekeeping cohort)."""

    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    page_route: Mapped[str | None] = mapped_column(Text, nullable=True)
    legacy_sqlite_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("feedback_user_idx", "user_id"),)


class Waitlist(Base):
    """Pre-launch waitlist signup (email-keyed lead). Migrated from SQLite in
    Sub-project G (housekeeping cohort)."""

    __tablename__ = "waitlist"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    practice_area: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
