"""Shared team model (Step-2 SQLite→PG cutover).

Minimal `teams` table introduced alongside the `Client` model so the
``clients.team_id -> teams.id`` foreign key has a resolvable target (the FK
target must exist for ``Base.metadata.create_all()`` and for the paired
Alembic migration to resolve). Team owns a UUID surrogate PK to match the
identity keyspace (``users.id``).

``TeamMember`` (membership + ``role``) ships here too, paired with the
``team_members`` table in ``20260621_step2_clients.py``. Remaining team
semantics (billing, the invite-acceptance flow beyond the ``invite_token`` /
``accepted_at`` columns) land in later Step-2 tasks and extend these models.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .case import UUIDType  # SQLite-roundtrip-safe UUID TypeDecorator INSTANCE


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


class TeamMember(Base):
    __tablename__ = "team_members"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="viewer")
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    invite_token: Mapped[str | None] = mapped_column(Text, unique=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="team_members_team_user_unique"),
        Index("team_members_user_idx", "user_id"),
        Index("team_members_team_idx", "team_id"),
    )
