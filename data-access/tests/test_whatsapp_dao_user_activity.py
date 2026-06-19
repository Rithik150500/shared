import uuid
from datetime import datetime, timedelta, timezone

from data_access.daos import user_dao, whatsapp_dao
from data_access.models import MessageLog
from data_access.models.whatsapp import WhatsAppDeliveryLog


def _munshi_user(session, phone="+919000000001"):
    user, _ = user_dao.get_or_create_by_phone(session, phone=phone, locale="en")
    user_dao.ensure_munshi_extension(session, user.id)
    session.flush()
    return user


def test_list_deliveries_for_user_orders_desc(db_session):
    u = _munshi_user(db_session)
    now = datetime.now(timezone.utc)
    for i, tmpl in enumerate(["a", "b", "c"]):
        db_session.add(WhatsAppDeliveryLog(
            id=uuid.uuid4(), user_id=u.id, template_name=tmpl, brand="munshi",
            delivery_status="sent", enqueued_at=now - timedelta(minutes=10 - i)))
    db_session.flush()
    rows = whatsapp_dao.list_deliveries_for_user(db_session, user_id=u.id, limit=10)
    assert [r.template_name for r in rows] == ["c", "b", "a"]


def test_list_inbound_for_user_by_user_id(db_session):
    u = _munshi_user(db_session)
    now = datetime.now(timezone.utc)
    db_session.add(MessageLog(id=uuid.uuid4(), meta_message_id=f"wamid.{uuid.uuid4().hex}",
                              user_phone=u.phone, user_id=u.id, received_at=now,
                              handler_name="onboarding"))
    db_session.flush()
    rows = whatsapp_dao.list_inbound_for_user(db_session, user_id=u.id, limit=10)
    assert len(rows) == 1
    assert rows[0].handler_name == "onboarding"
