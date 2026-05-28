from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# Postgres UUID with a String(36) fallback so Base.metadata.create_all() works
# on SQLite (tests). Mirrors the convention in models/user.py.
UUIDType = UUID(as_uuid=True).with_variant(String(36), "sqlite")


class Client(Base):
    """A Nowlez client (a law practice's client). Migrated from the legacy
    SQLite ``clients`` table in Sub-project G; cases / uploaded_files /
    chat_history all hang off a client.
    """

    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # References the forthcoming ``teams`` table. The FK constraint is added with
    # the teams cohort, so this stays a bare nullable UUID for now (a client is
    # shared to a team only on the paid tiers).
    team_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)

    # Sub-project G forensic column: the pre-G SQLite client id, so later cohorts
    # (uploaded_files, chat_history) can resolve client_id -> Postgres UUID.
    legacy_sqlite_id: Mapped[str | None] = mapped_column(
        Text, nullable=True, unique=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("clients_user_id_idx", "user_id"),)
