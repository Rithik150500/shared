import uuid
from datetime import datetime, timedelta, timezone

from data_access.daos import whatsapp_dao
from data_access.models import MessageLog


def _add_message(session, *, received_at):
    session.add(MessageLog(
        id=uuid.uuid4(),
        meta_message_id=f"wamid.{uuid.uuid4().hex}",
        user_phone="+919000000001",
        received_at=received_at,
    ))
    session.flush()


def test_count_messages_since(db_session):
    now = datetime.now(timezone.utc)
    _add_message(db_session, received_at=now)
    _add_message(db_session, received_at=now - timedelta(days=2))
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    assert whatsapp_dao.count_messages_since(db_session, start_today) == 1
