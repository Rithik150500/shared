import uuid
from datetime import datetime, timezone

from data_access.daos import user_dao, billing_dao
from data_access.models.billing import MunshiInvoice


def test_list_munshi_invoices_for_user(db_session):
    u, _ = user_dao.get_or_create_by_phone(db_session, phone="+919000000001", locale="en")
    now = datetime.now(timezone.utc)
    db_session.add(MunshiInvoice(id=uuid.uuid4(), user_id=u.id, cycle_start=now, cycle_end=now,
                                 case_count=3, amount_paise=3000, status="pending"))
    db_session.flush()
    rows = billing_dao.list_munshi_invoices_for_user(db_session, user_id=u.id, limit=12)
    assert len(rows) == 1
    assert rows[0].amount_paise == 3000
