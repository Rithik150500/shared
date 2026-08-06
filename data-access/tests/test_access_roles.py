"""One role ladder for both surfaces (2026-08-06).

The WhatsApp bot enforced NO roles: every mutating handler keyed on user.id.
That was survivable only while the bot could not see anyone else's book. Making
bot reads team-aware without a gate would let a team member ``/forget`` cases
out of the owner's book.

These tests pin the two properties that matter:
  1. the ladder matches casepilot's _ROLE_HIERARCHY (viewer < editor < owner),
     so a member is not an editor on the web and a viewer on WhatsApp;
  2. every ambiguous case fails CLOSED -- unknown role, unknown command,
     pending (unaccepted) invite, missing client.
"""
import uuid

import pytest

from data_access.access import (
    BOT_COMMAND_MIN_ROLE,
    ROLE_HIERARCHY,
    has_role,
    may_run_bot_command,
    role_for_case,
    role_for_client,
)
from data_access.daos import user_dao
from data_access.models import Client, Team, TeamMember


class TestLadder:
    def test_matches_the_web_hierarchy(self):
        assert ROLE_HIERARCHY == {"viewer": 0, "editor": 1, "owner": 2}

    @pytest.mark.parametrize(
        "role, min_role, allowed",
        [
            ("owner", "owner", True),
            ("owner", "editor", True),
            ("owner", "viewer", True),
            ("editor", "editor", True),
            ("editor", "viewer", True),
            ("editor", "owner", False),
            ("viewer", "viewer", True),
            ("viewer", "editor", False),
        ],
    )
    def test_ordering(self, role, min_role, allowed):
        assert has_role(role, min_role) is allowed

    @pytest.mark.parametrize("role", [None, "", "administrator", "OWNER"])
    def test_unknown_roles_fail_closed(self, role):
        assert has_role(role, "viewer") is False

    def test_forget_is_owner_only(self):
        """The one irreversible bot action has no undo; editors must not have it."""
        assert BOT_COMMAND_MIN_ROLE["forget"] == "owner"
        assert has_role("editor", BOT_COMMAND_MIN_ROLE["forget"]) is False


def _seed(session, *, accepted=True, role="editor"):
    """Owner with a team-shared client, plus a member and an outsider."""
    owner, _ = user_dao.get_or_create_by_phone(session, phone="+919811007330")
    member, _ = user_dao.get_or_create_by_phone(session, phone="+919811674912")
    outsider, _ = user_dao.get_or_create_by_phone(session, phone="+919999000111")
    session.flush()

    team = Team(id=uuid.uuid4(), name="Naseem Chambers", owner_id=owner.id)
    session.add(team)
    session.flush()

    session.add(
        TeamMember(
            id=uuid.uuid4(), team_id=team.id, user_id=member.id, role=role,
            accepted_at=(__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc) if accepted else None),
        )
    )
    client = Client(id="dd273e178b2d4384", user_id=owner.id,
                    name="Imported book", team_id=team.id)
    session.add(client)
    session.flush()
    return owner, member, outsider, client


class TestRoleForClient:
    def test_owner_is_owner(self, db_session):
        owner, _, _, client = _seed(db_session)
        assert role_for_client(
            db_session, client_id=client.id, user_id=owner.id) == "owner"

    def test_accepted_member_gets_their_role(self, db_session):
        _, member, _, client = _seed(db_session)
        assert role_for_client(
            db_session, client_id=client.id, user_id=member.id) == "editor"

    def test_pending_invite_grants_nothing(self, db_session):
        """accepted_at IS NULL is an invitation, not a membership."""
        _, member, _, client = _seed(db_session, accepted=False)
        assert role_for_client(
            db_session, client_id=client.id, user_id=member.id) is None

    def test_outsider_gets_nothing(self, db_session):
        _, _, outsider, client = _seed(db_session)
        assert role_for_client(
            db_session, client_id=client.id, user_id=outsider.id) is None

    def test_missing_client_denies(self, db_session):
        owner, _, _, _ = _seed(db_session)
        assert role_for_client(
            db_session, client_id="nope", user_id=owner.id) is None

    def test_garbage_user_id_denies(self, db_session):
        _, _, _, client = _seed(db_session)
        assert role_for_client(
            db_session, client_id=client.id, user_id="not-a-uuid") is None


class _FakeCase:
    def __init__(self, user_id, client_id=None):
        self.user_id = user_id
        self.client_id = client_id


class TestRoleForCaseAndBotGate:
    def test_shared_case_reaches_the_member(self, db_session):
        _, member, _, client = _seed(db_session)
        case = _FakeCase(user_id=uuid.uuid4(), client_id=client.id)
        assert role_for_case(
            db_session, case=case, user_id=member.id) == "editor"

    def test_clientless_case_is_owner_only(self, db_session):
        """Bot-saved cases belong to no shared book (10 of 1,319 fleet-wide)."""
        owner, member, _, _ = _seed(db_session)
        case = _FakeCase(user_id=owner.id, client_id=None)
        assert role_for_case(
            db_session, case=case, user_id=owner.id) == "owner"
        assert role_for_case(
            db_session, case=case, user_id=member.id) is None

    def test_editor_may_label_but_not_forget(self, db_session):
        """The whole point of the gate: reads widen, deletes do not."""
        _, member, _, client = _seed(db_session)
        case = _FakeCase(user_id=uuid.uuid4(), client_id=client.id)
        assert may_run_bot_command(
            db_session, command="label", case=case, user_id=member.id) is True
        assert may_run_bot_command(
            db_session, command="forget", case=case, user_id=member.id) is False

    def test_owner_may_forget(self, db_session):
        owner, _, _, client = _seed(db_session)
        case = _FakeCase(user_id=owner.id, client_id=client.id)
        assert may_run_bot_command(
            db_session, command="forget", case=case, user_id=owner.id) is True

    def test_outsider_may_do_nothing(self, db_session):
        _, _, outsider, client = _seed(db_session)
        case = _FakeCase(user_id=uuid.uuid4(), client_id=client.id)
        for cmd in BOT_COMMAND_MIN_ROLE:
            assert may_run_bot_command(
                db_session, command=cmd, case=case,
                user_id=outsider.id) is False, cmd

    def test_ungated_command_fails_closed(self, db_session):
        """A handler added later without a role entry must be refused, not allowed.

        This is the exact failure mode that let the bot ship with no gate.
        """
        owner, _, _, client = _seed(db_session)
        case = _FakeCase(user_id=owner.id, client_id=client.id)
        assert may_run_bot_command(
            db_session, command="some_new_command", case=case,
            user_id=owner.id) is False
