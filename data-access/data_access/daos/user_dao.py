from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import User, UserExternalIdentity, UserIdentity, UserMunshi, UserNowlez
from ..phone import normalize_phone

_IST = ZoneInfo("Asia/Kolkata")


class MergeUnsafeError(ValueError):
    """Raised by ``merge_users`` when the absorbed account owns irreplaceable
    child data (legal cases / billing subscriptions / a munshi bot identity)
    that the hard-delete would cascade away (those tables FK ``users.id`` with
    ondelete=CASCADE). The merge is refused rather than silently destroying
    data; the caller (D4 ``link_email_to_phone_account``) treats this as a
    merge_conflict requiring human resolution."""


def _resolve_verified_phone_alias(session: Session, phone: str) -> User | None:
    """Owner of ``phone`` as a VERIFIED phone alias, else None. ``phone`` must be
    E.164-normalized by the caller."""
    return session.execute(
        select(User)
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .where(
            UserIdentity.kind == "phone",
            UserIdentity.value == phone,
            UserIdentity.verified_at.is_not(None),
        )
    ).scalar_one_or_none()


def resolve_verified_email_alias(session: Session, email: str) -> User | None:
    """Owner of ``email`` as a VERIFIED email alias, else None. Canonicalizes the
    input (strip + lowercase) so callers may pass a raw address."""
    email = email.strip().lower()
    return session.execute(
        select(User)
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .where(
            UserIdentity.kind == "email",
            UserIdentity.value == email,
            UserIdentity.verified_at.is_not(None),
        )
    ).scalar_one_or_none()


def get_or_create_by_phone(
    session: Session, *, phone: str, locale: str = "en"
) -> tuple[User, bool]:
    """INSERT ON CONFLICT (phone) DO NOTHING then re-SELECT (dialect-aware,
    mirroring whatsapp_dao.claim_message). Fixes the read-then-write race at
    app.py:154 where two concurrent inbound workers could both INSERT the same
    phone. Returns (user, was_created)."""
    # Canonicalize to E.164 so the web/OTP path (bare 10-digit) and the WhatsApp
    # webhook (+91...) converge on one users row instead of splitting identity.
    phone = normalize_phone(phone)
    # A VERIFIED phone alias routes inbound/OTP to its OWNING account instead of
    # spawning a fresh user (closes the WhatsApp orphan-user footgun). Safe to
    # check before the insert: add_alias guarantees an alias value is never also a
    # live users.phone primary, so this and the ON CONFLICT path are mutually
    # exclusive.
    alias_owner = _resolve_verified_phone_alias(session, phone)
    if alias_owner is not None:
        return alias_owner, False
    dialect = session.get_bind().dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(User)
        .values(phone=phone, locale=locale)
        .on_conflict_do_nothing(index_elements=["phone"])
    )
    result = session.execute(stmt)
    session.flush()
    was_created = result.rowcount > 0
    user = session.execute(select(User).where(User.phone == phone)).scalar_one()
    return user, was_created


def get_by_phone(session: Session, phone: str) -> User | None:
    return session.execute(
        select(User).where(User.phone == normalize_phone(phone))
    ).scalar_one_or_none()


def get_by_id(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)


def ensure_munshi_extension(session: Session, user_id: uuid.UUID) -> UserMunshi:
    existing = session.get(UserMunshi, user_id)
    if existing is not None:
        return existing
    user = session.get(User, user_id)
    if user is None or user.phone is None:
        raise ValueError("ensure_munshi_extension requires user with non-null phone")
    # billing_anniversary_date is NOT NULL on Postgres (sub-project E billing).
    # Anchor a new user's billing anniversary to their signup date (IST), matching
    # the migration backfill (created_at::date) and case_billing's creation path.
    # Without this, the INSERT violates the NOT NULL constraint in prod and every
    # brand-new user (e.g. a broadcast recipient) fails to onboard.
    ext = UserMunshi(
        user_id=user_id,
        billing_anniversary_date=datetime.now(timezone.utc).astimezone(_IST).date(),
    )
    session.add(ext)
    session.flush()
    return ext


def ensure_nowlez_extension(session: Session, user_id: uuid.UUID, *, name: str) -> UserNowlez:
    existing = session.get(UserNowlez, user_id)
    if existing is not None:
        return existing
    ext = UserNowlez(user_id=user_id, name=name)
    session.add(ext)
    session.flush()
    return ext


def update_password(session: Session, user_id: uuid.UUID, password_hash: str) -> None:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")
    user.password_hash = password_hash
    session.flush()


def touch_last_login(session: Session, user_id: uuid.UUID) -> None:
    from sqlalchemy import func
    user = session.get(User, user_id)
    if user is not None:
        user.last_login_at = func.now()
        session.flush()


def set_active(session: Session, user_id: uuid.UUID, is_active: bool) -> None:
    user = session.get(User, user_id)
    if user is not None:
        user.is_active = is_active
        session.flush()


def has_munshi_extension(session: Session, user_id: uuid.UUID) -> bool:
    """True iff the user has a row in users_munshi (i.e. is a Munshi user)."""
    return session.get(UserMunshi, user_id) is not None


def has_nowlez_extension(session: Session, user_id: uuid.UUID) -> bool:
    """True iff the user has a row in users_nowlez (i.e. is a Nowlez user)."""
    return session.get(UserNowlez, user_id) is not None


def count_munshi_users(session: Session) -> int:
    return session.execute(select(func.count()).select_from(UserMunshi)).scalar_one()


def count_munshi_onboarded(session: Session) -> int:
    return session.execute(
        select(func.count()).select_from(UserMunshi).where(UserMunshi.onboarded_at.is_not(None))
    ).scalar_one()


def count_munshi_active_since(session: Session, since: datetime) -> int:
    """Count Munshi users whose last_message_at >= since (rows with NULL last_message_at are excluded)."""
    return session.execute(
        select(func.count()).select_from(UserMunshi).where(UserMunshi.last_message_at >= since)
    ).scalar_one()


def list_munshi_users(
    session: Session, *, limit: int = 50, offset: int = 0, search: str | None = None
) -> list[tuple[User, UserMunshi]]:
    stmt = select(User, UserMunshi).join(UserMunshi, UserMunshi.user_id == User.id)
    if search:
        stmt = stmt.where(User.phone.ilike(f"%{search}%"))
    stmt = stmt.order_by(UserMunshi.created_at.desc()).limit(limit).offset(offset)
    return list(session.execute(stmt).tuples().all())


def get_or_create_by_email(
    session: Session, *, email: str, locale: str = "en"
) -> tuple[User, bool]:
    """INSERT ON CONFLICT (email) DO NOTHING then re-SELECT (dialect-aware,
    mirroring whatsapp_dao.claim_message). ``email`` MUST be pre-canonicalized
    by the caller. Returns (user, was_created)."""
    dialect = session.get_bind().dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(User)
        .values(email=email, locale=locale)
        .on_conflict_do_nothing(index_elements=["email"])
    )
    result = session.execute(stmt)
    session.flush()
    was_created = result.rowcount > 0
    user = session.execute(select(User).where(User.email == email)).scalar_one()
    return user, was_created


def get_by_email(session: Session, email: str) -> User | None:
    return session.execute(select(User).where(User.email == email)).scalar_one_or_none()


def set_email_verified(session: Session, user_id: uuid.UUID) -> None:
    """Mark this account's email as verified (D4 signal). No-op if no
    users_nowlez extension exists yet — the caller ensures the extension first
    on the mint path."""
    session.execute(
        update(UserNowlez)
        .where(UserNowlez.user_id == user_id)
        .values(email_verified=True)
    )
    session.flush()


def is_email_verified(session: Session, user_id: uuid.UUID) -> bool:
    """True iff users_nowlez.email_verified is set for this user. A missing
    extension row reads as False (unverified)."""
    val = session.execute(
        select(UserNowlez.email_verified).where(UserNowlez.user_id == user_id)
    ).scalar_one_or_none()
    return bool(val)


def get_by_google_sub(session: Session, google_sub: str) -> User | None:
    """Resolve the users row linked to this Google subject id (the stable login
    anchor). Returns None if no Google identity has been linked for this sub."""
    return session.execute(
        select(User)
        .join(UserExternalIdentity, UserExternalIdentity.user_id == User.id)
        .where(
            UserExternalIdentity.provider == "google",
            UserExternalIdentity.provider_sub == google_sub,
        )
    ).scalar_one_or_none()


def link_google_identity(
    session: Session, *, user_id: uuid.UUID, google_sub: str, email: str | None = None
) -> bool:
    """Attach a Google identity to ``user_id``. Idempotent: a re-login for an
    already-linked (provider, sub) is a no-op (INSERT ... ON CONFLICT DO NOTHING,
    dialect-aware, mirroring get_or_create_by_email). ``email`` is the
    provider-asserted address stored for audit only.

    Returns True iff a NEW link row was inserted; False when the insert was a
    no-op (the sub is already linked, or this user already has a Google identity)
    so the caller can surface the skip.
    """
    dialect = session.get_bind().dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(UserExternalIdentity)
        .values(
            user_id=user_id,
            provider="google",
            provider_sub=google_sub,
            email=email,
        )
        # No index_elements: DO NOTHING on EITHER unique key
        # ((provider, provider_sub) or (user_id, provider)) so a repeat link or a
        # second Google account on the same user is a safe no-op, never an error.
        # NOTE: the untargeted ON CONFLICT DO NOTHING needs SQLite >= 3.35.0
        # (2021); Postgres is unaffected. All supported runtimes ship newer.
        .on_conflict_do_nothing()
    )
    result = session.execute(stmt)
    session.flush()
    return (result.rowcount or 0) > 0


def merge_users(session: Session, *, survivor_id: uuid.UUID, absorbed_id: uuid.UUID) -> None:
    """D4 auto-merge: fold the absorbed user into the survivor. Survivor is the
    canonical row (caller passes the older created_at as survivor). Never drops
    case/billing rows — only re-points the nowlez extension and copies the
    email/email_verified anchor, then deletes the absorbed users row (its
    leftover extension is removed by the ondelete=CASCADE FK)."""
    survivor = session.get(User, survivor_id)
    absorbed = session.get(User, absorbed_id)
    if survivor is None or absorbed is None or survivor_id == absorbed_id:
        return

    # SAFETY GUARD (D4): the absorbed row is hard-deleted below, and cases,
    # billing subscriptions, and the munshi (bot) identity all FK users.id with
    # ondelete=CASCADE. Refuse to merge — never silently destroy — an absorbed
    # account that owns any of that irreplaceable data (this also covers the
    # "survivor=older happens to be the data-light row" case). The caller treats
    # MergeUnsafeError as a merge_conflict requiring human resolution.
    from ..models import Case, Subscription

    absorbed_cases = session.execute(
        select(func.count()).select_from(Case).where(Case.user_id == absorbed_id)
    ).scalar_one()
    absorbed_subs = session.execute(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.user_id == absorbed_id)
    ).scalar_one()
    absorbed_has_munshi = session.get(UserMunshi, absorbed_id) is not None
    if absorbed_cases or absorbed_subs or absorbed_has_munshi:
        raise MergeUnsafeError(
            f"refusing to merge user {absorbed_id}: absorbed account owns child "
            f"data (cases={absorbed_cases}, subscriptions={absorbed_subs}, "
            f"munshi={absorbed_has_munshi}) that ondelete=CASCADE would destroy"
        )

    # Fold the email anchor onto the survivor if it doesn't already have one.
    # Use Core UPDATE statements to avoid mixed UUID/str ORM identity-map sort
    # errors on SQLite (where the ON CONFLICT re-SELECT returns str PKs while
    # directly-constructed User rows carry uuid.UUID PKs).
    if survivor.email is None and absorbed.email is not None:
        absorbed_email = absorbed.email
        # Release UNIQUE on absorbed first, then claim on survivor.
        session.execute(update(User).where(User.id == absorbed_id).values(email=None))
        session.flush()
        session.execute(update(User).where(User.id == survivor_id).values(email=absorbed_email))
        session.flush()
        session.expire(survivor)
        session.expire(absorbed)
    if survivor.phone is None and absorbed.phone is not None:
        absorbed_phone = absorbed.phone
        session.execute(update(User).where(User.id == absorbed_id).values(phone=None))
        session.flush()
        session.execute(update(User).where(User.id == survivor_id).values(phone=absorbed_phone))
        session.flush()
        session.expire(survivor)
        session.expire(absorbed)

    # Re-point the nowlez extension to the survivor only if the survivor has none.
    survivor_ext = session.get(UserNowlez, survivor_id)
    absorbed_ext = session.get(UserNowlez, absorbed_id)
    if survivor_ext is None and absorbed_ext is not None:
        session.execute(
            update(UserNowlez)
            .where(UserNowlez.user_id == absorbed_id)
            .values(user_id=survivor_id)
        )
    # Carry the verified-email flag forward.
    if absorbed_ext is not None and absorbed_ext.email_verified:
        session.execute(
            update(UserNowlez)
            .where(UserNowlez.user_id == survivor_id)
            .values(email_verified=True)
        )
    session.flush()

    # Delete the absorbed row; CASCADE removes any extension still pointing at it.
    # Re-fetch absorbed fresh so the ORM delete uses a consistent PK type.
    absorbed_fresh = session.get(User, absorbed_id)
    if absorbed_fresh is not None:
        session.delete(absorbed_fresh)
        session.flush()
