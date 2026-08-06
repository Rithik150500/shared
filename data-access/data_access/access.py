"""One definition of "what may this user do on this book", for every surface.

casepilot's web API has enforced roles since teams shipped, via
``backend/helpers.py::_verify_access``. The WhatsApp bot has enforced nothing:
every mutating handler (``/forget``, ``/snooze``, ``/label``, bulk digest
toggles, ``/save``) keys on ``user.id`` with no role check at all. That was
survivable only because the bot could not see anyone else's book. The moment
bot reads become team-aware, the absence of a gate stops being a gap and
becomes a data-loss vector: a team member could ``/forget`` cases out of the
owner's book.

Duplicating the web's rule in the bot would give two definitions that drift --
which is exactly how aliases (bot) and teams (web) ended up as two unrelated
sharing mechanisms for the same idea. So the rule lives here, in the package
both backends already pin, and each surface keeps only its own way of refusing:
the web raises HTTPException(404), the bot replies with a message. This module
deliberately raises NOTHING framework-specific and returns plain data.

Semantics match ``_verify_access`` exactly:
  - direct ownership of the client  -> "owner"
  - accepted team membership        -> the member's role
  - anything else                   -> None (caller decides how to refuse)
Unaccepted invitations do NOT grant access; ``accepted_at IS NULL`` is a
pending invite, not a member.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_access.models import Case, Client, TeamMember

logger = logging.getLogger(__name__)

# Ordered capability ladder. Kept identical to casepilot's _ROLE_HIERARCHY;
# if these ever diverge, the two surfaces disagree about who may delete.
ROLE_HIERARCHY: dict[str, int] = {"viewer": 0, "editor": 1, "owner": 2}

#: Roles permitted to perform each bot mutation. Mirrors the web's ``min_role``
#: arguments so a member is not an editor on one surface and a viewer on the
#: other. ``forget`` is owner-only: it is the sole irreversible action, and an
#: editor deleting an owner's tracked case has no undo.
BOT_COMMAND_MIN_ROLE: dict[str, str] = {
    "forget": "owner",
    "digest_toggle_bulk": "owner",
    "save": "editor",
    "label": "editor",
    "snooze": "editor",
}


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def has_role(role: str | None, min_role: str) -> bool:
    """True when ``role`` sits at or above ``min_role`` on the ladder.

    An unknown role scores -1 and therefore fails every check -- fail closed, so
    a typo or a future role name added to the DB cannot silently grant rights.
    """
    if role is None:
        return False
    return ROLE_HIERARCHY.get(role, -1) >= ROLE_HIERARCHY.get(min_role, 99)


def role_for_client(
    session: Session, *, client_id: str, user_id: Any
) -> str | None:
    """Return this user's role on a client, or None if they cannot reach it."""
    uid = _as_uuid(user_id)
    if uid is None or not client_id:
        return None

    client = session.execute(
        select(Client).where(Client.id == client_id)
    ).scalar_one_or_none()
    if client is None:
        return None

    # str()-coerce both sides: client.user_id may be a UUID object or a string
    # depending on which backend wrote the row.
    if str(client.user_id) == str(uid):
        return "owner"

    if client.team_id is None:
        return None

    member = session.execute(
        select(TeamMember).where(
            TeamMember.team_id == client.team_id,
            TeamMember.user_id == uid,
        )
    ).scalar_one_or_none()
    if member is None or member.accepted_at is None:
        return None
    return member.role


def role_for_case(session: Session, *, case: Any, user_id: Any) -> str | None:
    """Return this user's role on the book a case belongs to.

    A case with no ``client_id`` was saved through the bot and belongs to no
    shared book, so it is reachable only by its owner. That covers 10 of 1,319
    cases fleet-wide as of 2026-08-06 -- rare, but they must not become
    invisible OR unexpectedly shared, hence the explicit ownership branch.
    """
    uid = _as_uuid(user_id)
    if uid is None or case is None:
        return None

    client_id = getattr(case, "client_id", None)
    if not client_id:
        return "owner" if str(getattr(case, "user_id", "")) == str(uid) else None

    return role_for_client(session, client_id=client_id, user_id=uid)


def may_run_bot_command(
    session: Session, *, command: str, case: Any, user_id: Any
) -> bool:
    """Gate a bot mutation against the same ladder the web enforces.

    Unknown commands fail closed: a handler added later without a
    ``BOT_COMMAND_MIN_ROLE`` entry is refused rather than silently ungated,
    which is the failure mode that let the bot ship with no gate at all.
    """
    min_role = BOT_COMMAND_MIN_ROLE.get(command)
    if min_role is None:
        logger.warning(
            "bot command %r has no role requirement; refusing (fail closed)",
            command,
        )
        return False
    return has_role(role_for_case(session, case=case, user_id=user_id), min_role)
