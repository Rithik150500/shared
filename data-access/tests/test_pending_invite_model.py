import uuid
from data_access.models import PendingTeamInvite, Team, User


def test_pending_invite_roundtrip(db_session):
    o = User(email="o@x.com", is_active=True); db_session.add(o); db_session.flush()
    tid = uuid.uuid4()
    db_session.add(Team(id=tid, owner_id=o.id, name="T")); db_session.flush()
    p = PendingTeamInvite(
        invite_token="tok-123", team_id=tid, email="invitee@x.com",
        role="viewer", invited_by=o.id,
    )
    db_session.add(p); db_session.flush()
    got = db_session.get(PendingTeamInvite, "tok-123")
    assert got.email == "invitee@x.com"
    assert got.last_email_sent_at is None
