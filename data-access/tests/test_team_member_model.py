import uuid
import pytest
from sqlalchemy.exc import IntegrityError

from data_access.models import Team, TeamMember, User


def _u(s, email):
    u = User(email=email, is_active=True); s.add(u); s.flush(); return u


def test_team_member_roundtrip_and_pending(db_session):
    o = _u(db_session, "o@x.com"); tid = uuid.uuid4()
    db_session.add(Team(id=tid, owner_id=o.id, name="T")); db_session.flush()
    m = TeamMember(team_id=tid, user_id=o.id, role="owner", accepted_at=None)
    db_session.add(m); db_session.flush()
    assert isinstance(m.id, uuid.UUID)        # UUID PK, not int
    assert m.accepted_at is None               # NULL == pending preserved


def test_team_member_unique_team_user(db_session):
    o = _u(db_session, "o2@x.com"); tid = uuid.uuid4()
    db_session.add(Team(id=tid, owner_id=o.id, name="T")); db_session.flush()
    db_session.add(TeamMember(team_id=tid, user_id=o.id, role="owner"))
    db_session.flush()
    db_session.add(TeamMember(team_id=tid, user_id=o.id, role="viewer"))
    with pytest.raises(IntegrityError):
        db_session.flush()
