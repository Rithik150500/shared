from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data_access.models import NotificationNowlez


def upsert(s: Session, *, legacy_sqlite_id: int, client_id: str, user_id: uuid.UUID,
           dedup_key: str | None, case_cnr: str | None, type: str, title: str,
           message: str, old_value: str | None = None, new_value: str | None = None,
           is_read: bool = False) -> uuid.UUID:
    """Idempotent on dedup_key when non-NULL: a second upsert with the same
    non-NULL dedup_key is a no-op returning the existing row id. NULL dedup_key
    never collapses (NULLs are distinct)."""
    if dedup_key is not None:
        existing = s.execute(select(NotificationNowlez).where(
            NotificationNowlez.dedup_key == dedup_key)).scalar_one_or_none()
        if existing is not None:
            return existing.id
    row = NotificationNowlez(
        legacy_sqlite_id=legacy_sqlite_id, client_id=client_id, user_id=user_id,
        dedup_key=dedup_key, case_cnr=case_cnr, type=type, title=title,
        message=message, old_value=old_value, new_value=new_value, is_read=is_read)
    s.add(row); s.flush()
    return row.id


def mark_read(s: Session, *, legacy_sqlite_id: int) -> bool:
    row = s.execute(select(NotificationNowlez).where(
        NotificationNowlez.legacy_sqlite_id == legacy_sqlite_id)).scalar_one_or_none()
    if row is None:
        return False
    row.is_read = True; s.flush()
    return True


def mark_all_read(s: Session, *, user_id: uuid.UUID) -> int:
    rows = s.execute(select(NotificationNowlez).where(
        NotificationNowlez.user_id == user_id,
        NotificationNowlez.is_read.is_(False))).scalars().all()
    for r in rows:
        r.is_read = True
    s.flush()
    return len(rows)


def list_for_user(s: Session, *, user_id: uuid.UUID, limit: int) -> list[NotificationNowlez]:
    return list(s.execute(select(NotificationNowlez).where(
        NotificationNowlez.user_id == user_id)
        .order_by(NotificationNowlez.created_at.desc()).limit(limit)).scalars().all())


def count_unread(s: Session, *, user_id: uuid.UUID) -> int:
    return int(s.execute(select(func.count()).select_from(NotificationNowlez).where(
        NotificationNowlez.user_id == user_id,
        NotificationNowlez.is_read.is_(False))).scalar_one())


def count_unread_new_orders(s: Session, *, user_id: uuid.UUID,
                            client_id: str | None = None) -> int:
    q = select(func.count()).select_from(NotificationNowlez).where(
        NotificationNowlez.user_id == user_id,
        NotificationNowlez.is_read.is_(False),
        NotificationNowlez.type == "new_orders")
    if client_id is not None:
        q = q.where(NotificationNowlez.client_id == client_id)
    return int(s.execute(q).scalar_one())


def verify_ownership(s: Session, *, legacy_sqlite_id: int, user_id: uuid.UUID) -> bool:
    return s.execute(select(NotificationNowlez.id).where(
        NotificationNowlez.legacy_sqlite_id == legacy_sqlite_id,
        NotificationNowlez.user_id == user_id)).scalar_one_or_none() is not None


def list_for_case(s: Session, *, client_id: str, case_cnr: str) -> list[NotificationNowlez]:
    return list(s.execute(select(NotificationNowlez).where(
        NotificationNowlez.client_id == client_id,
        NotificationNowlez.case_cnr == case_cnr)).scalars().all())


def delete_by_client_cnr(s: Session, *, client_id: str, case_cnr: str) -> int:
    if case_cnr is None:
        raise ValueError("delete_by_client_cnr requires a real case_cnr (NULL-cnr survives delete_case)")
    rows = s.execute(select(NotificationNowlez).where(
        NotificationNowlez.client_id == client_id,
        NotificationNowlez.case_cnr == case_cnr)).scalars().all()
    for r in rows:
        s.delete(r)
    s.flush()
    return len(rows)


def delete_by_client(s: Session, *, client_id: str) -> int:
    rows = s.execute(select(NotificationNowlez).where(
        NotificationNowlez.client_id == client_id)).scalars().all()
    for r in rows:
        s.delete(r)
    s.flush()
    return len(rows)
