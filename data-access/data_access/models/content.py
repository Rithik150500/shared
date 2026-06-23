"""Step-4 content/notifications cohort models (uploaded_files / chat_history /
notifications), shared by Munshi and Nowlez.

search_tsv GENERATED columns + GIN indexes are Postgres-only and live in the
migration (20260622_step4_content); they are NOT declared here so the SQLite
create_all test variant does not choke on tsvector.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .case import JSONBType, UUIDType  # the shared instances (case.py:60-61)


class UploadedFileNowlez(Base):
    __tablename__ = "uploaded_files_nowlez"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    legacy_sqlite_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    client_id: Mapped[str] = mapped_column(
        Text, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    descriptive_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cnr: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_storage: Mapped[str] = mapped_column(Text, nullable=False, default="local")
    r2_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    r2_etag: Mapped[str | None] = mapped_column(Text, nullable=True)  # content ETag, served on 304 (Task 11)
    preprocessed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    permanently_failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # search_tsv: PG generated column added in the migration (Task 2); not declared
    # here so the SQLite create_all test variant does not choke on tsvector.

    __table_args__ = (
        CheckConstraint("file_storage IN ('local','r2')", name="uploaded_files_nowlez_storage_check"),
        CheckConstraint("file_storage <> 'r2' OR r2_object_key IS NOT NULL",
                        name="uploaded_files_nowlez_r2_key_present"),
        Index("idx_uploaded_files_nowlez_client_id", "client_id"),
        Index("idx_uploaded_files_nowlez_cnr", "cnr"),
    )


class ChatHistoryNowlez(Base):
    __tablename__ = "chat_history_nowlez"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    legacy_sqlite_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    # NON-ENFORCED FK / plain TEXT: allows the unified synthetic key (no clients row).
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sources_json: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    function_calls_json: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("feedback IS NULL OR feedback IN ('up','down')",
                        name="chat_history_nowlez_feedback_check"),
        Index("idx_chat_history_nowlez_client_id", "client_id"),
        Index("idx_chat_history_nowlez_created_at", "client_id", "created_at"),
    )


class NotificationNowlez(Base):
    __tablename__ = "notifications_nowlez"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    legacy_sqlite_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    client_id: Mapped[str] = mapped_column(
        Text, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)  # denormalized owner
    case_cnr: Mapped[str | None] = mapped_column(Text, nullable=True)  # NOT an FK
    type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dedup_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("dedup_key", name="notifications_nowlez_dedup_key_uniq"),
        Index("idx_notifications_nowlez_user_id", "user_id", "created_at"),
        Index("idx_notifications_nowlez_case", "client_id", "case_cnr"),
    )
