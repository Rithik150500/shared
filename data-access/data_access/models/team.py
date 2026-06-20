"""Shared team model (Step-2 SQLite→PG cutover).

Minimal `teams` table introduced alongside the `Client` model so the
``clients.team_id -> teams.id`` foreign key has a resolvable target (the FK
target must exist for ``Base.metadata.create_all()`` and for the paired
Alembic migration to resolve). Team owns a UUID surrogate PK to match the
identity keyspace (``users.id``); richer team semantics (membership, roles,
billing) land in later Step-2 tasks and extend this model.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .user import UUIDType  # SQLite-roundtrip-safe UUID TypeDecorator INSTANCE


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, primary_key=True, default=uuid.uuid4,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(
        Text, nullable=False, default="free", server_default="free",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("teams_owner_id_idx", "owner_id"),
    )
