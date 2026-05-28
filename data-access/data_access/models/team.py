from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# Postgres UUID with a String(36) fallback for SQLite (tests). See models/user.py.
UUIDType = UUID(as_uuid=True).with_variant(String(36), "sqlite")


class Team(Base):
    """A Nowlez team (firm) for shared client access (paid tiers). Migrated from
    the legacy SQLite ``teams`` table in Sub-project G."""

    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tier: Mapped[str] = mapped_column(Text, nullable=False, default="free", server_default="free")
    # Forensic: pre-G SQLite team id, so team_members / clients.team_id resolve.
    legacy_sqlite_id: Mapped[str | None] = mapped_column(
        Text, nullable=True, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("teams_owner_id_idx", "owner_id"),)


class TeamMember(Base):
    """Membership of a user in a team with an RBAC role. UNIQUE(team_id, user_id)
    is the idempotency key for the migration (the SQLite autoincrement id is
    dropped in favour of a UUID PK)."""

    __tablename__ = "team_members"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, default="viewer", server_default="viewer")
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    invite_token: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="team_members_team_user_uq"),
        Index("team_members_user_idx", "user_id"),
    )


class PendingTeamInvite(Base):
    """Email invite to a team for an address that isn't a user yet. Keyed by the
    invite token (its SQLite primary key)."""

    __tablename__ = "pending_team_invites"

    invite_token: Mapped[str] = mapped_column(Text, primary_key=True)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="viewer", server_default="viewer")
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("pending_team_invites_email_idx", "email"),)
