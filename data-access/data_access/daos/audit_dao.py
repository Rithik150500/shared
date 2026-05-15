from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from ..models import AuditLog


def log_event(
    session: Session,
    *,
    event_type: str,
    source: str,
    user_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    metadata: dict | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    a = AuditLog(
        event_type=event_type,
        source=source,
        user_id=user_id,
        actor_id=actor_id,
        metadata_=metadata or {},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.add(a)
    session.flush()
    return a
