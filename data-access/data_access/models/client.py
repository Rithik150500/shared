"""Shared client-management model (Step-2 SQLite→PG cutover).

clients keeps its 16-hex TEXT natural key verbatim from SQLite (locked
decision: PG cases.client_id already stores the 16-hex string as a
denormalized passthrough — keeping the key avoids a shim rewrite).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .case import UUIDType  # SQLite-roundtrip-safe UUID TypeDecorator INSTANCE


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # 16-hex, verbatim
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("teams.id", ondelete="SET NULL"), nullable=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    is_demo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("clients_user_id_idx", "user_id"),
        Index("clients_user_created_idx", "user_id", text("created_at DESC")),
        Index(
            "clients_team_id_idx", "team_id",
            postgresql_where=text("team_id IS NOT NULL"),
        ),
    )
