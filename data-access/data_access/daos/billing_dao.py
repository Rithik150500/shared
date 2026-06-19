from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.billing import MunshiInvoice


def list_munshi_invoices_for_user(session: Session, *, user_id: uuid.UUID, limit: int = 12):
    """Munshi invoices for a user, newest cycle first."""
    stmt = (select(MunshiInvoice)
            .where(MunshiInvoice.user_id == user_id)
            .order_by(MunshiInvoice.cycle_end.desc())
            .limit(limit))
    return list(session.execute(stmt).scalars().all())
