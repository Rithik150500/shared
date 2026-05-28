from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# Postgres UUID with a String(36) fallback for SQLite (tests). See models/user.py.
UUIDType = UUID(as_uuid=True).with_variant(String(36), "sqlite")


class UploadedFile(Base):
    """A user-uploaded document, scoped to a client. Migrated from the legacy
    SQLite ``uploaded_files`` table in Sub-project G (content cohort)."""

    __tablename__ = "uploaded_files"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    descriptive_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cnr: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    preprocessed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    permanently_failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    # Forensic: pre-G SQLite row id (str), used as the migration idempotency key.
    legacy_sqlite_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("uploaded_files_client_idx", "client_id"),)


class ChatHistory(Base):
    """A single chat message. Per-client chats set ``client_id``; unified
    (cross-client) chats leave it NULL and rely on ``user_id``. Migrated from the
    legacy SQLite ``chat_history`` table (where unified rows were keyed by the
    ``__unified__{user_id}`` sentinel client_id)."""

    __tablename__ = "chat_history"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("clients.id", ondelete="CASCADE"), nullable=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sources_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    function_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    legacy_sqlite_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("chat_history_user_idx", "user_id", "created_at"),
        Index("chat_history_client_idx", "client_id"),
    )
