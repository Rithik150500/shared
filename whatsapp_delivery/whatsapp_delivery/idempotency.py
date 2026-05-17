"""Meta retries any unacknowledged webhook for up to 24h. We dedupe on
``meta_message_id``.

Post-F: claim_message takes UUID user_id, not phone. The user_id is resolved
upstream in app.py via user_dao.get_or_create_by_phone.
"""
from __future__ import annotations

import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from data_access.models.whatsapp import MessageLog


def claim_message(session: Session, *, meta_message_id: str, user_id: uuid.UUID) -> bool:
    """Return True if this is the first sighting; False on Meta retry."""
    dialect = session.get_bind().dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = insert_fn(MessageLog).values(
        meta_message_id=meta_message_id,
        user_id=user_id,
    ).on_conflict_do_nothing(index_elements=["meta_message_id"])
    result = session.execute(stmt)
    session.commit()
    return result.rowcount > 0
