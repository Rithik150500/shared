import uuid
from datetime import datetime

from data_access.models import Client, Team, User


def test_client_roundtrip_sqlite(db_session):
    u = User(email="o@x.com", is_active=True)
    db_session.add(u); db_session.flush()
    c = Client(
        id="abc123def456aaaa", user_id=u.id, name="Acme",
        email="c@x.com", phone="+919000000000", notes="n", is_demo=False,
    )
    db_session.add(c); db_session.flush()
    got = db_session.get(Client, "abc123def456aaaa")
    assert got.user_id == u.id
    assert got.team_id is None
    assert got.is_demo is False
    assert isinstance(got.created_at, datetime)


def test_client_team_fk_nullable(db_session):
    u = User(email="o2@x.com", is_active=True)
    db_session.add(u); db_session.flush()
    t = Team(id=uuid.uuid4(), owner_id=u.id, name="T", tier="free")
    db_session.add(t); db_session.flush()
    c = Client(id="bbb222ccc333dddd", user_id=u.id, name="WithTeam", team_id=t.id)
    db_session.add(c); db_session.flush()
    assert db_session.get(Client, "bbb222ccc333dddd").team_id == t.id
