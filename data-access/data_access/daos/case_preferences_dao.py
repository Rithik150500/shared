"""DAO for the case_preferences table.

Thin CRUD wrapper. Re-save protection lives at the caller (save_case's
short-circuit at handlers/monitoring/save_command.py:134); the DAO's
upsert unconditionally updates whatever columns the caller passes
explicitly (alert_level=None means "don't change in UPDATE clause").
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from data_access.models.case_preferences import CasePreferences


def get_by_cnr(
    s: Session, *, user_id: uuid.UUID, cnr: str,
) -> CasePreferences | None:
    """Fetch one row by composite PK; returns None if absent."""
    stmt = select(CasePreferences).where(
        CasePreferences.user_id == user_id,
        CasePreferences.cnr == cnr,
    )
    return s.execute(stmt).scalar_one_or_none()


def list_for_user(
    s: Session, *, user_id: uuid.UUID,
) -> list[CasePreferences]:
    """All preferences for a user, oldest first."""
    stmt = (
        select(CasePreferences)
        .where(CasePreferences.user_id == user_id)
        .order_by(CasePreferences.created_at.asc())
    )
    return list(s.execute(stmt).scalars())


def upsert(
    s: Session,
    *,
    user_id: uuid.UUID,
    cnr: str,
    alert_level: str | None = None,
    snooze_until: datetime | None = None,
    digest_enabled: bool | None = None,
) -> CasePreferences:
    """INSERT-or-UPDATE the preferences row.

    Only updates columns where the caller passed a non-None value. On a
    fresh INSERT, omitted columns fall back to their defaults
    (alert_level='all', digest_enabled=True, snooze_until=NULL).
    """
    insert_values: dict = {
        "user_id": user_id,
        "cnr": cnr,
    }
    if alert_level is not None:
        insert_values["alert_level"] = alert_level
    if snooze_until is not None:
        insert_values["snooze_until"] = snooze_until
    if digest_enabled is not None:
        insert_values["digest_enabled"] = digest_enabled

    # Build UPDATE clause: only columns the caller explicitly passed (excluding
    # PK columns), plus the updated_at bump.
    update_values: dict = {}
    if alert_level is not None:
        update_values["alert_level"] = alert_level
    if snooze_until is not None:
        update_values["snooze_until"] = snooze_until
    if digest_enabled is not None:
        update_values["digest_enabled"] = digest_enabled

    dialect = s.bind.dialect.name if s.bind is not None else "postgresql"
    if dialect == "sqlite":
        stmt = sqlite_insert(CasePreferences).values(**insert_values)
    else:
        stmt = pg_insert(CasePreferences).values(**insert_values)

    if update_values:
        # Bump updated_at via Python-side datetime — server_default doesn't
        # apply to UPDATE in either dialect, and `onupdate=func.now()` on
        # the column only fires for ORM-level updates, not Core inserts.
        update_values["updated_at"] = datetime.utcnow()
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "cnr"],
            set_=update_values,
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "cnr"])

    s.execute(stmt)
    s.flush()
    # Re-fetch to return the canonical row (handles both INSERT and UPDATE paths).
    return get_by_cnr(s, user_id=user_id, cnr=cnr)  # type: ignore[return-value]


def delete(s: Session, *, user_id: uuid.UUID, cnr: str) -> int:
    """Delete one row by composite PK; returns rowcount (0 or 1)."""
    from sqlalchemy import delete as sa_delete
    stmt = sa_delete(CasePreferences).where(
        CasePreferences.user_id == user_id,
        CasePreferences.cnr == cnr,
    )
    result = s.execute(stmt)
    s.flush()
    return result.rowcount or 0
