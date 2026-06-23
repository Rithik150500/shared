from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_access.models import UploadedFileNowlez, Client

_INSERT_FIELDS = (
    "original_filename", "descriptive_name", "summary", "page_count", "cnr",
    "document_type", "file_path", "file_storage", "r2_object_key", "r2_etag",
    "preprocessed", "retry_count", "permanently_failed",
)
_UPDATABLE_FIELDS = frozenset((
    "descriptive_name", "summary", "page_count", "cnr", "document_type",
    "file_path", "file_storage", "r2_object_key", "r2_etag", "preprocessed",
    "retry_count", "permanently_failed", "original_filename",
))


def insert(s: Session, *, legacy_sqlite_id: int, client_id: str,
           original_filename: str, file_path: str, **cols: Any) -> uuid.UUID:
    row = UploadedFileNowlez(
        legacy_sqlite_id=legacy_sqlite_id, client_id=client_id,
        original_filename=original_filename, file_path=file_path,
        **{k: v for k, v in cols.items() if k in _INSERT_FIELDS})
    s.add(row); s.flush()
    return row.id


def update_fields(s: Session, *, legacy_sqlite_id: int, **cols: Any) -> bool:
    unknown = set(cols) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_fields: unknown column(s) {sorted(unknown)}")
    row = get_by_legacy_id(s, legacy_sqlite_id=legacy_sqlite_id)
    if row is None:
        return False
    for k, v in cols.items():
        setattr(row, k, v)
    s.flush()
    return True


def rename(s: Session, *, legacy_sqlite_id: int, original_filename: str,
           file_path: str, descriptive_name: str | None = None) -> bool:
    # descriptive_name is OPTIONAL: the SQLite source rename_uploaded_file
    # (backend/db/files.py:220) supplies only filename + path, so when the caller
    # omits descriptive_name we LEAVE IT UNCHANGED (do not overwrite to NULL).
    fields = dict(original_filename=original_filename, file_path=file_path)
    if descriptive_name is not None:
        fields["descriptive_name"] = descriptive_name
    return update_fields(s, legacy_sqlite_id=legacy_sqlite_id, **fields)


def delete(s: Session, *, legacy_sqlite_id: int) -> None:
    row = get_by_legacy_id(s, legacy_sqlite_id=legacy_sqlite_id)
    if row is not None:
        s.delete(row); s.flush()


def delete_by_client_cnr(s: Session, *, client_id: str, cnr: str) -> int:
    if cnr is None:
        raise ValueError("delete_by_client_cnr requires a real cnr (NULL-cnr rows survive delete_case)")
    rows = s.execute(select(UploadedFileNowlez).where(
        UploadedFileNowlez.client_id == client_id,
        UploadedFileNowlez.cnr == cnr)).scalars().all()
    for r in rows:
        s.delete(r)
    s.flush()
    return len(rows)


def delete_by_client(s: Session, *, client_id: str) -> int:
    rows = s.execute(select(UploadedFileNowlez).where(
        UploadedFileNowlez.client_id == client_id)).scalars().all()
    for r in rows:
        s.delete(r)
    s.flush()
    return len(rows)


def get_by_legacy_id(s: Session, *, legacy_sqlite_id: int) -> UploadedFileNowlez | None:
    return s.execute(select(UploadedFileNowlez).where(
        UploadedFileNowlez.legacy_sqlite_id == legacy_sqlite_id)).scalar_one_or_none()


def get_by_path(s: Session, *, client_id: str, file_path: str) -> UploadedFileNowlez | None:
    return s.execute(select(UploadedFileNowlez).where(
        UploadedFileNowlez.client_id == client_id,
        UploadedFileNowlez.file_path == file_path)).scalar_one_or_none()


def list_for_client(s: Session, *, client_id: str) -> list[UploadedFileNowlez]:
    return list(s.execute(select(UploadedFileNowlez).where(
        UploadedFileNowlez.client_id == client_id).order_by(
        UploadedFileNowlez.created_at)).scalars().all())


def get_failed(s: Session, *, user_id: uuid.UUID | None = None) -> list[UploadedFileNowlez]:
    q = select(UploadedFileNowlez).join(Client, Client.id == UploadedFileNowlez.client_id).where(
        UploadedFileNowlez.preprocessed.is_(False),
        UploadedFileNowlez.permanently_failed.is_(False))
    if user_id is not None:
        q = q.where(Client.user_id == user_id)
    return list(s.execute(q).scalars().all())


def recent_for_user(s: Session, *, user_id: uuid.UUID, n: int) -> list[UploadedFileNowlez]:
    return list(s.execute(
        select(UploadedFileNowlez).join(Client, Client.id == UploadedFileNowlez.client_id)
        .where(Client.user_id == user_id)
        .order_by(UploadedFileNowlez.created_at.desc()).limit(n)).scalars().all())
