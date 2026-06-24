from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_access.models import ChatHistoryNowlez


def insert(s: Session, *, legacy_sqlite_id: int, client_id: str, role: str,
           content: str, sources_json, function_calls_json) -> uuid.UUID:
    row = ChatHistoryNowlez(
        legacy_sqlite_id=legacy_sqlite_id, client_id=client_id, role=role,
        content=content, sources_json=sources_json,
        function_calls_json=function_calls_json)
    s.add(row); s.flush()
    return row.id


def update_content(s: Session, *, legacy_sqlite_id: int, content: str) -> bool:
    row = s.execute(select(ChatHistoryNowlez).where(
        ChatHistoryNowlez.legacy_sqlite_id == legacy_sqlite_id)).scalar_one_or_none()
    if row is None:
        return False
    row.content = content; s.flush()
    return True


def set_feedback(s: Session, *, legacy_sqlite_id: int, feedback: str | None) -> bool:
    row = s.execute(select(ChatHistoryNowlez).where(
        ChatHistoryNowlez.legacy_sqlite_id == legacy_sqlite_id)).scalar_one_or_none()
    if row is None:
        return False
    row.feedback = feedback; s.flush()
    return True


def delete_after(s: Session, *, client_id: str, after_legacy_id: int) -> int:
    rows = s.execute(select(ChatHistoryNowlez).where(
        ChatHistoryNowlez.client_id == client_id,
        ChatHistoryNowlez.legacy_sqlite_id > after_legacy_id)).scalars().all()
    for r in rows:
        s.delete(r)
    s.flush()
    return len(rows)


def delete_by_client(s: Session, *, client_id: str) -> int:
    rows = s.execute(select(ChatHistoryNowlez).where(
        ChatHistoryNowlez.client_id == client_id)).scalars().all()
    for r in rows:
        s.delete(r)
    s.flush()
    return len(rows)


def list_for_client(s: Session, *, client_id: str, limit: int) -> list[ChatHistoryNowlez]:
    """Return the most-recent ``limit`` rows in chronological (oldest-first) order.

    Mirrors the legacy SQLite ``ORDER BY id DESC LIMIT ?`` + reverse window so the
    PG read serves the same newest-N slice the LLM-context path relies on (NOT the
    oldest N). Selects newest-first via DESC, then reverses to chronological.
    """
    rows = s.execute(select(ChatHistoryNowlez).where(
        ChatHistoryNowlez.client_id == client_id)
        .order_by(ChatHistoryNowlez.created_at.desc(),
                  ChatHistoryNowlez.legacy_sqlite_id.desc())
        .limit(limit)).scalars().all()
    return list(reversed(rows))


def list_paginated(s: Session, *, client_id: str, offset: int, limit: int) -> list[ChatHistoryNowlez]:
    return list(s.execute(select(ChatHistoryNowlez).where(
        ChatHistoryNowlez.client_id == client_id)
        .order_by(ChatHistoryNowlez.created_at, ChatHistoryNowlez.legacy_sqlite_id)
        .offset(offset).limit(limit)).scalars().all())
