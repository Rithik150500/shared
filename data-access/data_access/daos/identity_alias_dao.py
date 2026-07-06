from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import Case, Subscription, User, UserIdentity
from ..phone import normalize_phone


class AliasConflictError(ValueError):
    """Raised by ``add_alias`` when the requested value already belongs to
    another account that owns irreplaceable data (cases / subscriptions). The
    add is refused rather than reclaiming (deleting) that account."""


def _canonicalize(kind: str, value: str) -> str:
    if kind == "phone":
        return normalize_phone(value)
    if kind == "email":
        return value.strip().lower()
    raise ValueError(f"unsupported identity kind: {kind!r}")


def _primary_owner_id(session: Session, kind: str, value: str) -> uuid.UUID | None:
    col = User.phone if kind == "phone" else User.email
    return session.execute(
        select(User.id).where(col == value)
    ).scalar_one_or_none()


def _alias_row(session: Session, kind: str, value: str) -> UserIdentity | None:
    return session.execute(
        select(UserIdentity).where(
            UserIdentity.kind == kind, UserIdentity.value == value
        )
    ).scalar_one_or_none()


def _owns_legal_data(session: Session, user_id: uuid.UUID) -> bool:
    """True iff the account owns cases or subscriptions — the irreplaceable data
    that makes it NOT a reclaimable empty orphan. A users_munshi bot-state row
    alone is transient and does NOT count (unlike merge_users, which guards on
    munshi because it hard-deletes on a different code path)."""
    cases = session.execute(
        select(func.count()).select_from(Case).where(Case.user_id == user_id)
    ).scalar_one()
    subs = session.execute(
        select(func.count()).select_from(Subscription).where(Subscription.user_id == user_id)
    ).scalar_one()
    return bool(cases or subs)


def add_alias(
    session: Session,
    *,
    user_id: uuid.UUID,
    kind: str,
    value: str,
    verified: bool = False,
    added_by: str = "operator",
    reclaim_orphan: bool = True,
) -> UserIdentity | None:
    """Attach ``value`` (phone/email) as an identity of ``user_id``.

    Returns the created/updated ``UserIdentity``. Returns ``None`` when ``value``
    is already this account's own primary (nothing to add). On collision with
    ANOTHER account: reclaim it (delete) iff it owns no cases/subscriptions and
    ``reclaim_orphan``; otherwise raise ``AliasConflictError``.
    """
    value = _canonicalize(kind, value)
    verified_at = datetime.now(timezone.utc) if verified else None

    # (a) Already this account's own primary -> nothing to alias.
    primary_owner = _primary_owner_id(session, kind, value)
    if primary_owner == user_id:
        return None

    # (b) Existing alias row for this value.
    existing = _alias_row(session, kind, value)
    if existing is not None and existing.user_id == user_id:
        if verified and existing.verified_at is None:
            existing.verified_at = verified_at
            session.flush()
        return existing

    # (c) Collision with another account (as primary OR alias) -> reclaim/refuse.
    colliding_id = primary_owner or (existing.user_id if existing else None)
    if colliding_id is not None and colliding_id != user_id:
        if _owns_legal_data(session, colliding_id) or not reclaim_orphan:
            raise AliasConflictError(
                f"{kind} {value!r} belongs to account {colliding_id} which cannot "
                f"be reclaimed (owns_legal_data="
                f"{_owns_legal_data(session, colliding_id)}, reclaim={reclaim_orphan})"
            )
        # Empty orphan: hard-delete it (CASCADE clears its munshi/nowlez/alias
        # rows and frees the primary value), then fall through to insert.
        orphan = session.get(User, colliding_id)
        if orphan is not None:
            session.delete(orphan)
            session.flush()

    row = UserIdentity(
        user_id=user_id, kind=kind, value=value,
        verified_at=verified_at, added_by=added_by,
    )
    session.add(row)
    session.flush()
    return row


def verify_alias(session: Session, alias_id: uuid.UUID) -> None:
    session.execute(
        update(UserIdentity)
        .where(UserIdentity.id == alias_id)
        .values(verified_at=datetime.now(timezone.utc))
    )
    session.flush()


def remove_alias(session: Session, alias_id: uuid.UUID) -> None:
    row = session.get(UserIdentity, alias_id)
    if row is not None:
        session.delete(row)
        session.flush()


def list_aliases(session: Session, user_id: uuid.UUID) -> list[UserIdentity]:
    return list(
        session.execute(
            select(UserIdentity)
            .where(UserIdentity.user_id == user_id)
            .order_by(UserIdentity.created_at)
        ).scalars().all()
    )
