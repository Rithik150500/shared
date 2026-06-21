import uuid

from data_access.models import Team, User


def test_team_roundtrip_sqlite(db_session):
    u = User(email="owner@x.com", is_active=True)
    db_session.add(u); db_session.flush()
    tid = uuid.uuid4()
    t = Team(id=tid, owner_id=u.id, name="Chambers", tier="chambers")
    db_session.add(t); db_session.flush()
    got = db_session.get(Team, tid)
    assert got.owner_id == u.id
    assert got.tier == "chambers"
