from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import (
    AuditLog,
    Case,
    CaseBillingPeriod,
    CasePreferences,
    Client,
    LoginRequest,
    MunshiInvoice,
    MunshiUpsellEvent,
    NotificationNowlez,
    PendingTeamInvite,
    Referral,
    Subscription,
    Team,
    TeamMember,
    User,
    UserExternalIdentity,
    UserIdentity,
    UserMunshi,
    UserNowlez,
    WhatsAppDeliveryLog,
)
from ..models.auth import AuthSession
from ..models.whatsapp import MessageLog
from ..phone import normalize_phone
from . import audit_dao

_IST = ZoneInfo("Asia/Kolkata")


class MergeUnsafeError(ValueError):
    """Raised by ``merge_users`` when the absorbed account owns irreplaceable
    child data (legal cases / billing subscriptions / a munshi bot identity)
    that the hard-delete would cascade away (those tables FK ``users.id`` with
    ondelete=CASCADE). The merge is refused rather than silently destroying
    data; the caller (D4 ``link_email_to_phone_account``) treats this as a
    merge_conflict requiring human resolution."""


class MergeConflictError(ValueError):
    """Raised by ``merge_users(repoint=True)`` when both the survivor and the
    absorbed account own a genuinely ambiguous 1:1 identity — today this is
    only ``users_munshi`` (both have a distinct Munshi bot identity; there is
    no safe way to pick a winner automatically). Distinct from
    ``MergeUnsafeError``: this is raised only in repoint mode, after the
    caller has already opted into moving data, and it means "even repoint
    mode can't resolve this without a human." ``plan_merge_repoint`` flags the
    same condition in its ``conflicts`` list so a dry-run surfaces it before
    the CLI ever calls ``merge_users(repoint=True)``."""


# Tables that FK users.id with ondelete=CASCADE and are safely re-pointed by
# a plain ``UPDATE ... SET user_id = :survivor WHERE user_id = :absorbed``.
# (table_name, model, user_id-column-name) — table_name is also the dict key
# in plan_merge_repoint's returned counts and the audit metadata.
#
# NOTE: case_preferences, team_members, and munshi_invoices are deliberately
# NOT here even though they FK users.id with CASCADE — each has a unique
# constraint that the survivor and absorbed can genuinely overlap on (the
# primary merge scenario is one human's two accounts, who naturally share
# data). A blind bulk UPDATE would raise IntegrityError on that overlap; those
# three are instead handled by the move-or-drop helpers below (see
# _repoint_case_preferences_dropping_dupes / _repoint_team_members_dropping_dupes
# / _repoint_munshi_invoices_dropping_dupes), mirroring the pre-existing
# user_external_identities / user_identities pattern.
_CLEAN_REPOINT_TABLES: tuple[tuple[str, type, str], ...] = (
    ("cases", Case, "user_id"),
    ("clients", Client, "user_id"),
    ("subscriptions", Subscription, "user_id"),
    ("case_billing_periods", CaseBillingPeriod, "user_id"),
    ("notifications_nowlez", NotificationNowlez, "user_id"),
    ("munshi_upsell_events", MunshiUpsellEvent, "user_id"),
    ("whatsapp_delivery_log", WhatsAppDeliveryLog, "user_id"),
    ("teams", Team, "owner_id"),
)

# referrals.referrer_user_id has NO unique constraint (many-per-user) so it is
# safe to blind-UPDATE unconditionally. referrals.referred_user_id DOES have a
# UniqueConstraint (a user can be referred at most once) and can genuinely
# overlap between survivor and absorbed, so it is handled by
# _repoint_referrals_dropping_dupes below instead of here.
_REFERRAL_BLIND_FK_COLUMNS: tuple[str, ...] = ("referrer_user_id",)

# Ephemeral tables: DELETE the absorbed's rows rather than re-point (the user
# simply re-authenticates under the survivor).
_EPHEMERAL_TABLES: tuple[tuple[str, type, str], ...] = (
    ("auth_sessions", AuthSession, "user_id"),
    ("login_requests", LoginRequest, "user_id"),
)

# SET NULL tables: re-point for attribution continuity. Never blocks the
# absorbed-row delete (the FK already tolerates NULL), so this is purely a
# "don't lose the paper trail" nicety, done best-effort inside the same
# transaction.
_SET_NULL_REPOINT_TABLES: tuple[tuple[str, type, str], ...] = (
    ("audit_log", AuditLog, "user_id"),
    ("audit_log", AuditLog, "actor_id"),
    ("message_log", MessageLog, "user_id"),
    ("team_members", TeamMember, "invited_by"),
    ("pending_team_invites", PendingTeamInvite, "invited_by"),
    ("users_nowlez", UserNowlez, "referred_by"),
)


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


def _count_case_preferences_move_drop(
    session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID
) -> tuple[int, int]:
    """(move_count, drop_count) for case_preferences, keyed on ``cnr`` — the
    composite PK is (user_id, cnr), so a row collides iff the survivor already
    has a case_preferences row for that same cnr."""
    absorbed_rows = session.execute(
        select(CasePreferences.cnr).where(CasePreferences.user_id == absorbed_id)
    ).scalars().all()
    move, drop = 0, 0
    for cnr in absorbed_rows:
        collides = session.get(CasePreferences, (survivor_id, cnr)) is not None
        drop += 1 if collides else 0
        move += 0 if collides else 1
    return move, drop


def _count_team_members_move_drop(
    session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID
) -> tuple[int, int]:
    """(move_count, drop_count) for team_members, keyed on ``team_id`` —
    UniqueConstraint(team_id, user_id), so a row collides iff the survivor is
    already a member of that same team."""
    absorbed_team_ids = session.execute(
        select(TeamMember.team_id).where(TeamMember.user_id == absorbed_id)
    ).scalars().all()
    move, drop = 0, 0
    for team_id in absorbed_team_ids:
        collides = session.execute(
            select(func.count())
            .select_from(TeamMember)
            .where(TeamMember.team_id == team_id, TeamMember.user_id == survivor_id)
        ).scalar_one()
        drop += 1 if collides else 0
        move += 0 if collides else 1
    return move, drop


def _count_referrals_move_drop(
    session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID
) -> tuple[int, int]:
    """(move_count, drop_count) for referrals.referred_user_id, keyed on mere
    existence — UniqueConstraint(referred_user_id) means a user can be
    referred at most once, so the absorbed's referred-row (if any) collides
    iff the survivor already has ANY referred-row of its own."""
    absorbed_has_referred_row = (
        session.execute(
            select(func.count())
            .select_from(Referral)
            .where(Referral.referred_user_id == absorbed_id)
        ).scalar_one()
        > 0
    )
    if not absorbed_has_referred_row:
        return 0, 0
    survivor_has_referred_row = (
        session.execute(
            select(func.count())
            .select_from(Referral)
            .where(Referral.referred_user_id == survivor_id)
        ).scalar_one()
        > 0
    )
    return (0, 1) if survivor_has_referred_row else (1, 0)


def _count_munshi_invoices_move_drop(
    session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID
) -> tuple[int, int]:
    """(move_count, drop_count) for munshi_invoices, keyed on
    ``(cycle_start, cycle_end)`` — UniqueConstraint(user_id, cycle_start,
    cycle_end), so a row collides iff the survivor already has an invoice for
    that exact billing cycle."""
    absorbed_rows = session.execute(
        select(MunshiInvoice.cycle_start, MunshiInvoice.cycle_end).where(
            MunshiInvoice.user_id == absorbed_id
        )
    ).all()
    move, drop = 0, 0
    for cycle_start, cycle_end in absorbed_rows:
        collides = session.execute(
            select(func.count())
            .select_from(MunshiInvoice)
            .where(
                MunshiInvoice.user_id == survivor_id,
                MunshiInvoice.cycle_start == cycle_start,
                MunshiInvoice.cycle_end == cycle_end,
            )
        ).scalar_one()
        drop += 1 if collides else 0
        move += 0 if collides else 1
    return move, drop


def _count_child_rows(session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID) -> dict:
    """Shared by plan_merge_repoint (dry-run) and merge_users' refuse-guard:
    per-table counts of absorbed-owned rows, plus the conflicts list. Reads
    only — never writes."""
    counts: dict[str, int] = {}
    for table_name, model, col in _CLEAN_REPOINT_TABLES:
        counts[table_name] = session.execute(
            select(func.count()).select_from(model).where(getattr(model, col) == absorbed_id)
        ).scalar_one()

    # case_preferences / team_members / munshi_invoices: same move-or-drop
    # shape as user_external_identities / user_identities below — each has a
    # unique constraint the survivor and absorbed can genuinely overlap on.
    cp_move, cp_drop = _count_case_preferences_move_drop(session, survivor_id, absorbed_id)
    counts["case_preferences"] = cp_move
    if cp_drop:
        counts["case_preferences_dropped_dupes"] = cp_drop

    tm_move, tm_drop = _count_team_members_move_drop(session, survivor_id, absorbed_id)
    counts["team_members"] = tm_move
    if tm_drop:
        counts["team_members_dropped_dupes"] = tm_drop

    mi_move, mi_drop = _count_munshi_invoices_move_drop(session, survivor_id, absorbed_id)
    counts["munshi_invoices"] = mi_move
    if mi_drop:
        counts["munshi_invoices_dropped_dupes"] = mi_drop

    # referrals: absorbed may appear as referrer (blind move, no collision
    # possible — no unique constraint) AND/OR referred (collision-checked via
    # _count_referrals_move_drop). Count is the number of DISTINCT rows that
    # would MOVE cleanly (referrer-hits always count; the referred-hit counts
    # only if it doesn't collide) plus the dropped-dupes count reported
    # separately, mirroring the other move-or-drop tables.
    referrer_row_ids = set(
        session.execute(
            select(Referral.id).where(Referral.referrer_user_id == absorbed_id)
        ).scalars().all()
    )
    referred_move, referred_drop = _count_referrals_move_drop(session, survivor_id, absorbed_id)
    referred_row_ids: set = set()
    if referred_move or referred_drop:
        referred_row_ids = set(
            session.execute(
                select(Referral.id).where(Referral.referred_user_id == absorbed_id)
            ).scalars().all()
        )
    # A row could theoretically hit both columns only if absorbed referred
    # itself, which the app never creates, but union guards against double-
    # counting if it ever did.
    counts["referrals"] = len(referrer_row_ids | (referred_row_ids if referred_move else set()))
    if referred_drop:
        counts["referrals_dropped_dupes"] = referred_drop

    conflicts: list[dict] = []

    survivor_has_munshi = session.get(UserMunshi, survivor_id) is not None
    absorbed_has_munshi = session.get(UserMunshi, absorbed_id) is not None
    counts["users_munshi"] = 1 if absorbed_has_munshi else 0
    if survivor_has_munshi and absorbed_has_munshi:
        conflicts.append(
            {
                "table": "users_munshi",
                "reason": "both survivor and absorbed own a distinct Munshi bot "
                "identity; automatic re-point would silently pick a winner",
            }
        )

    # user_external_identities: rows that would collide on (provider, sub).
    absorbed_ext = session.execute(
        select(UserExternalIdentity).where(UserExternalIdentity.user_id == absorbed_id)
    ).scalars().all()
    ext_move = 0
    ext_drop = 0
    for row in absorbed_ext:
        collides = session.execute(
            select(func.count())
            .select_from(UserExternalIdentity)
            .where(
                UserExternalIdentity.user_id == survivor_id,
                UserExternalIdentity.provider == row.provider,
                UserExternalIdentity.provider_sub == row.provider_sub,
            )
        ).scalar_one()
        if collides:
            ext_drop += 1
        else:
            ext_move += 1
    counts["user_external_identities"] = ext_move
    if ext_drop:
        counts["user_external_identities_dropped_dupes"] = ext_drop

    # user_identities: rows that would collide on (kind, value).
    absorbed_idents = session.execute(
        select(UserIdentity).where(UserIdentity.user_id == absorbed_id)
    ).scalars().all()
    ident_move = 0
    ident_drop = 0
    for row in absorbed_idents:
        collides = session.execute(
            select(func.count())
            .select_from(UserIdentity)
            .where(
                UserIdentity.user_id == survivor_id,
                UserIdentity.kind == row.kind,
                UserIdentity.value == row.value,
            )
        ).scalar_one()
        if collides:
            ident_drop += 1
        else:
            ident_move += 1
    counts["user_identities"] = ident_move
    if ident_drop:
        counts["user_identities_dropped_dupes"] = ident_drop

    # users_nowlez: existing merge_users semantics — re-pointed only if the
    # survivor has none; otherwise the absorbed's extension is dropped
    # (CASCADE-deleted along with the absorbed row). Not a "conflict" — this
    # is long-standing, deliberate, non-blocking behavior.
    counts["users_nowlez"] = (
        1
        if session.get(UserNowlez, absorbed_id) is not None
        and session.get(UserNowlez, survivor_id) is None
        else 0
    )

    return {"counts": counts, "conflicts": conflicts}


def plan_merge_repoint(session: Session, *, survivor_id: uuid.UUID, absorbed_id: uuid.UUID) -> dict:
    """Dry-run planner for ``merge_users(repoint=True)``. Returns per-table
    counts of rows that WOULD move from ``absorbed_id`` to ``survivor_id``,
    plus a ``conflicts`` list flagging any genuinely ambiguous 1:1 state (see
    ``MergeConflictError``). Writes NOTHING — this is read-only and safe to
    call speculatively (e.g. to print a CLI dry-run preview).

    The returned dict is ``{**per_table_counts, "conflicts": [...]}`` so CLI
    callers can iterate everything except the "conflicts" key as a table.
    """
    result = _count_child_rows(session, survivor_id, absorbed_id)
    return {**result["counts"], "conflicts": result["conflicts"]}


def _repoint_clean_tables(session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID) -> dict:
    """UPDATE every clean-repoint CASCADE child table's user_id (or owner_id
    for teams) from absorbed->survivor. Returns the per-table row counts
    actually moved (for the audit event).

    NOTE: referrals.referred_user_id is deliberately NOT blind-updated here —
    it has a UniqueConstraint the survivor and absorbed can genuinely overlap
    on, so it's handled by _repoint_referrals_dropping_dupes instead.
    referrer_user_id has no such constraint (many-per-user) and is safe to
    blind-update unconditionally.
    """
    moved: dict[str, int] = {}
    for table_name, model, col in _CLEAN_REPOINT_TABLES:
        result = session.execute(
            update(model).where(getattr(model, col) == absorbed_id).values(**{col: survivor_id})
        )
        moved[table_name] = moved.get(table_name, 0) + (result.rowcount or 0)

    referral_moved = 0
    for col in _REFERRAL_BLIND_FK_COLUMNS:
        result = session.execute(
            update(Referral).where(getattr(Referral, col) == absorbed_id).values(**{col: survivor_id})
        )
        referral_moved += result.rowcount or 0
    moved["referrals"] = referral_moved
    session.flush()
    return moved


def _repoint_case_preferences_dropping_dupes(
    session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID
) -> dict:
    """case_preferences (composite PK (user_id, cnr)): re-point rows that
    don't collide with something the survivor already owns for that cnr;
    DELETE the absorbed's row when it does collide (the survivor's row wins).
    Mirrors _repoint_identity_tables_dropping_dupes's shape exactly."""
    from sqlalchemy import delete

    moved = 0
    dropped = 0
    absorbed_rows = session.execute(
        select(CasePreferences).where(CasePreferences.user_id == absorbed_id)
    ).scalars().all()
    for row in absorbed_rows:
        collides = session.get(CasePreferences, (survivor_id, row.cnr)) is not None
        if collides:
            session.execute(
                delete(CasePreferences).where(
                    CasePreferences.user_id == absorbed_id, CasePreferences.cnr == row.cnr,
                )
            )
            dropped += 1
        else:
            session.execute(
                update(CasePreferences)
                .where(CasePreferences.user_id == absorbed_id, CasePreferences.cnr == row.cnr)
                .values(user_id=survivor_id)
            )
            moved += 1
    session.flush()
    return {"moved": {"case_preferences": moved}, "dropped": {"case_preferences": dropped}}


def _repoint_team_members_dropping_dupes(
    session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID
) -> dict:
    """team_members (UniqueConstraint(team_id, user_id)): re-point rows that
    don't collide with a membership the survivor already has on that team;
    DELETE the absorbed's row when it does collide (the survivor's
    membership wins). Mirrors _repoint_identity_tables_dropping_dupes's
    shape exactly."""
    from sqlalchemy import delete

    moved = 0
    dropped = 0
    absorbed_rows = session.execute(
        select(TeamMember).where(TeamMember.user_id == absorbed_id)
    ).scalars().all()
    for row in absorbed_rows:
        collides = session.execute(
            select(func.count())
            .select_from(TeamMember)
            .where(TeamMember.team_id == row.team_id, TeamMember.user_id == survivor_id)
        ).scalar_one()
        if collides:
            session.execute(delete(TeamMember).where(TeamMember.id == row.id))
            dropped += 1
        else:
            session.execute(
                update(TeamMember).where(TeamMember.id == row.id).values(user_id=survivor_id)
            )
            moved += 1
    session.flush()
    return {"moved": {"team_members": moved}, "dropped": {"team_members": dropped}}


def _repoint_referrals_dropping_dupes(
    session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID
) -> dict:
    """referrals.referred_user_id (UniqueConstraint(referred_user_id) — a user
    can be referred at most once): re-point the absorbed's referred-row iff
    the survivor doesn't already have one of its own; DELETE it when the
    survivor already has a referred-row (the survivor's wins). Mirrors
    _repoint_identity_tables_dropping_dupes's shape exactly.

    referrer_user_id is handled separately by the unconditional blind UPDATE
    in _repoint_clean_tables (no unique constraint there, no collision risk).
    """
    from sqlalchemy import delete

    moved = 0
    dropped = 0
    absorbed_referred_row = session.execute(
        select(Referral).where(Referral.referred_user_id == absorbed_id)
    ).scalar_one_or_none()
    if absorbed_referred_row is not None:
        collides = session.execute(
            select(func.count())
            .select_from(Referral)
            .where(Referral.referred_user_id == survivor_id)
        ).scalar_one()
        if collides:
            session.execute(
                delete(Referral).where(Referral.id == absorbed_referred_row.id)
            )
            dropped += 1
        else:
            session.execute(
                update(Referral)
                .where(Referral.id == absorbed_referred_row.id)
                .values(referred_user_id=survivor_id)
            )
            moved += 1
    session.flush()
    return {"moved": {"referrals": moved}, "dropped": {"referrals": dropped}}


def _repoint_munshi_invoices_dropping_dupes(
    session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID
) -> dict:
    """munshi_invoices (UniqueConstraint(user_id, cycle_start, cycle_end)):
    re-point rows that don't collide with an invoice the survivor already has
    for that exact billing cycle; DELETE the absorbed's row when it does
    collide (the survivor's invoice wins). Mirrors
    _repoint_identity_tables_dropping_dupes's shape exactly."""
    from sqlalchemy import delete

    moved = 0
    dropped = 0
    absorbed_rows = session.execute(
        select(MunshiInvoice).where(MunshiInvoice.user_id == absorbed_id)
    ).scalars().all()
    for row in absorbed_rows:
        collides = session.execute(
            select(func.count())
            .select_from(MunshiInvoice)
            .where(
                MunshiInvoice.user_id == survivor_id,
                MunshiInvoice.cycle_start == row.cycle_start,
                MunshiInvoice.cycle_end == row.cycle_end,
            )
        ).scalar_one()
        if collides:
            session.execute(delete(MunshiInvoice).where(MunshiInvoice.id == row.id))
            dropped += 1
        else:
            session.execute(
                update(MunshiInvoice).where(MunshiInvoice.id == row.id).values(user_id=survivor_id)
            )
            moved += 1
    session.flush()
    return {"moved": {"munshi_invoices": moved}, "dropped": {"munshi_invoices": dropped}}


def _delete_ephemeral_tables(session: Session, absorbed_id: uuid.UUID) -> dict:
    """DELETE (not re-point) the absorbed's ephemeral rows — the user simply
    re-authenticates under the survivor. Returns per-table deleted counts."""
    from sqlalchemy import delete

    deleted: dict[str, int] = {}
    for table_name, model, col in _EPHEMERAL_TABLES:
        result = session.execute(delete(model).where(getattr(model, col) == absorbed_id))
        deleted[table_name] = result.rowcount or 0
    session.flush()
    return deleted


def _repoint_set_null_tables(session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID) -> dict:
    """Best-effort re-point of SET NULL attribution columns (audit_log,
    message_log, team_members.invited_by, pending_team_invites.invited_by) so
    the paper trail survives the merge. Never blocks the delete — these
    columns already tolerate NULL — this is purely for continuity."""
    moved: dict[str, int] = {}
    for table_name, model, col in _SET_NULL_REPOINT_TABLES:
        result = session.execute(
            update(model).where(getattr(model, col) == absorbed_id).values(**{col: survivor_id})
        )
        key = f"{table_name}.{col}"
        moved[key] = result.rowcount or 0
    session.flush()
    return moved


def _repoint_munshi_extension(session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID) -> bool:
    """Re-point users_munshi absorbed->survivor iff only the absorbed has one.
    Raises MergeConflictError if BOTH have one (caller has already checked
    this isn't the case in the common path, but this is the authoritative,
    race-safe check done inside the transaction). Returns True iff a row was
    moved."""
    survivor_has = session.get(UserMunshi, survivor_id) is not None
    absorbed_has = session.get(UserMunshi, absorbed_id) is not None
    if survivor_has and absorbed_has:
        raise MergeConflictError(
            f"cannot repoint-merge user {absorbed_id} into {survivor_id}: both "
            "accounts own a distinct users_munshi (Munshi bot) identity — "
            "genuine ambiguity, refusing to guess a winner"
        )
    if absorbed_has and not survivor_has:
        session.execute(
            update(UserMunshi).where(UserMunshi.user_id == absorbed_id).values(user_id=survivor_id)
        )
        session.flush()
        return True
    return False


def _repoint_identity_tables_dropping_dupes(
    session: Session, survivor_id: uuid.UUID, absorbed_id: uuid.UUID
) -> dict:
    """user_external_identities (UNIQUE provider+sub) and user_identities
    (UNIQUE kind+value): re-point rows that don't collide with something the
    survivor already owns; DELETE the absorbed's row when it does collide
    (the survivor already has that identity — the absorbed's copy is a inert
    duplicate, never a distinct fact worth preserving). Never violates the
    unique constraint because colliding rows are deleted, not updated.

    NOTE on staleness: these are Core UPDATE/DELETE statements, which mutate
    rows at the SQL level without refreshing any ORM objects already in the
    session's identity map. ``merge_users`` calls ``session.expire_all()``
    after every repoint helper (including this one) runs, so callers never
    observe stale attributes — see that call site's comment for why a
    narrower per-row ``session.expire()`` is not sufficient on these
    particular tables (their PK column isn't the roundtrip-safe UUID
    TypeDecorator, so a freshly-SELECTed row can fail to dedupe against an
    already-loaded object for the same primary key).
    """
    from sqlalchemy import delete

    moved = {"user_external_identities": 0, "user_identities": 0}
    dropped = {"user_external_identities": 0, "user_identities": 0}

    absorbed_ext = session.execute(
        select(UserExternalIdentity).where(UserExternalIdentity.user_id == absorbed_id)
    ).scalars().all()
    for row in absorbed_ext:
        collides = session.execute(
            select(func.count())
            .select_from(UserExternalIdentity)
            .where(
                UserExternalIdentity.user_id == survivor_id,
                UserExternalIdentity.provider == row.provider,
                UserExternalIdentity.provider_sub == row.provider_sub,
            )
        ).scalar_one()
        if collides:
            session.execute(delete(UserExternalIdentity).where(UserExternalIdentity.id == row.id))
            dropped["user_external_identities"] += 1
        else:
            session.execute(
                update(UserExternalIdentity)
                .where(UserExternalIdentity.id == row.id)
                .values(user_id=survivor_id)
            )
            moved["user_external_identities"] += 1

    absorbed_idents = session.execute(
        select(UserIdentity).where(UserIdentity.user_id == absorbed_id)
    ).scalars().all()
    for row in absorbed_idents:
        collides = session.execute(
            select(func.count())
            .select_from(UserIdentity)
            .where(
                UserIdentity.user_id == survivor_id,
                UserIdentity.kind == row.kind,
                UserIdentity.value == row.value,
            )
        ).scalar_one()
        if collides:
            session.execute(delete(UserIdentity).where(UserIdentity.id == row.id))
            dropped["user_identities"] += 1
        else:
            session.execute(
                update(UserIdentity).where(UserIdentity.id == row.id).values(user_id=survivor_id)
            )
            moved["user_identities"] += 1

    session.flush()
    return {"moved": moved, "dropped": dropped}


def merge_users(
    session: Session,
    *,
    survivor_id: uuid.UUID,
    absorbed_id: uuid.UUID,
    repoint: bool = False,
) -> None:
    """D4 auto-merge: fold the absorbed user into the survivor. Survivor is the
    canonical row (caller passes the older created_at as survivor).

    ``repoint=False`` (the default, UNCHANGED behavior): never drops
    case/billing rows — only re-points the nowlez extension and copies the
    email/email_verified anchor, then deletes the absorbed users row. Raises
    ``MergeUnsafeError`` if the absorbed owns cases/subscriptions/a munshi
    identity (the hard-delete's ondelete=CASCADE would destroy them). This is
    the only mode the login-time auto-merge (``link_email_to_phone_account``)
    ever calls.

    ``repoint=True`` (Phase-2 sub-project D, admin-CLI-only, NEVER called
    automatically from login): instead of refusing, MOVES every CASCADE
    child table's rows from absorbed->survivor in this one transaction
    (all-or-nothing — a mid-move exception leaves NOTHING changed, since
    nothing here commits/rollbacks its own transaction; the caller's
    transaction boundary governs atomicity), then runs the same anchor/
    extension logic and deletes the now-childless absorbed row. Raises
    ``MergeConflictError`` (distinct from MergeUnsafeError) if both accounts
    own a users_munshi identity — a genuine ambiguity even repoint mode
    won't guess through. Logs an ``account.merge_repointed`` audit event
    with the per-table moved-counts.
    """
    survivor = session.get(User, survivor_id)
    absorbed = session.get(User, absorbed_id)
    if survivor is None or absorbed is None or survivor_id == absorbed_id:
        return

    if not repoint:
        # SAFETY GUARD (D4): the absorbed row is hard-deleted below, and cases,
        # billing subscriptions, and the munshi (bot) identity all FK users.id
        # with ondelete=CASCADE. Refuse to merge — never silently destroy — an
        # absorbed account that owns any of that irreplaceable data (this also
        # covers the "survivor=older happens to be the data-light row" case).
        # The caller treats MergeUnsafeError as a merge_conflict requiring
        # human resolution.
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
        _merge_anchors_and_delete(session, survivor, absorbed, survivor_id, absorbed_id)
        return

    # REPOINT MODE: everything from here — the child-table moves, the
    # existing anchor/extension fold, and the absorbed-row delete — runs
    # inside ONE SAVEPOINT (session.begin_nested()) so a mid-move exception
    # rolls back ALL of it, leaving the session exactly as the caller found
    # it. A plain try/except without a SAVEPOINT is not enough here: on
    # SQLAlchemy, an exception raised after a flush() does not by itself undo
    # that flush unless something calls rollback()/nested-rollback, and a
    # bare session.rollback() would ALSO discard whatever unrelated work the
    # caller had pending before calling merge_users. begin_nested() scopes
    # the rollback to exactly this operation.
    with session.begin_nested():
        # Hard conflict check runs FIRST so we fail fast before touching any
        # other table (cheap correctness: no point moving 10 tables' worth of
        # rows only to discover the munshi identity can't be resolved).
        _repoint_munshi_extension(session, survivor_id, absorbed_id)
        moved_counts: dict = {}
        moved_counts.update(_repoint_clean_tables(session, survivor_id, absorbed_id))
        ident_result = _repoint_identity_tables_dropping_dupes(session, survivor_id, absorbed_id)
        moved_counts.update(ident_result["moved"])
        dropped_duplicates: dict = dict(ident_result["dropped"])

        # case_preferences / team_members / referrals(referred_user_id) /
        # munshi_invoices: same move-or-drop pattern as the identity tables
        # above — each has a unique constraint the survivor and absorbed can
        # genuinely overlap on (the primary merge scenario is one human's two
        # accounts, who naturally share data), so a blind bulk UPDATE would
        # raise IntegrityError. The survivor's row wins; the absorbed's
        # colliding row is dropped instead of moved.
        for collision_helper in (
            _repoint_case_preferences_dropping_dupes,
            _repoint_team_members_dropping_dupes,
            _repoint_referrals_dropping_dupes,
            _repoint_munshi_invoices_dropping_dupes,
        ):
            collision_result = collision_helper(session, survivor_id, absorbed_id)
            for table_name, count in collision_result["moved"].items():
                moved_counts[table_name] = moved_counts.get(table_name, 0) + count
            for table_name, count in collision_result["dropped"].items():
                if count:
                    dropped_duplicates[table_name] = dropped_duplicates.get(table_name, 0) + count

        moved_counts["_dropped_duplicates"] = dropped_duplicates
        moved_counts["_ephemeral_deleted"] = _delete_ephemeral_tables(session, absorbed_id)
        moved_counts["_set_null_repointed"] = _repoint_set_null_tables(session, survivor_id, absorbed_id)

        # The Core UPDATE statements above (in _repoint_clean_tables /
        # _repoint_identity_tables_dropping_dupes / the collision helpers /
        # _repoint_set_null_tables)
        # mutate rows at the SQL level without the ORM's unit-of-work knowing
        # to refresh already-loaded Python objects. Worse, on the
        # with_variant(String(36), "sqlite") columns used by several of these
        # models (not the roundtrip-safe TypeDecorator Case/CasePreferences
        # use), a fresh SELECT's row can even fail to dedupe against an
        # already-identity-mapped object for the SAME primary key (str vs.
        # uuid.UUID PK values hash differently), so per-row session.expire()
        # calls on the freshly-selected row are not sufficient — a caller
        # holding an earlier reference (e.g. a test fixture, or another
        # in-flight request in the same session) would still see stale data.
        # expire_all() is the blunt, always-correct fix: every ORM object in
        # the identity map is marked stale and will re-SELECT on next access.
        session.expire_all()

        _merge_anchors_and_delete(session, survivor, absorbed, survivor_id, absorbed_id)

        # Audit deltas: the pre-repoint audit_dao.log_event calls were one-way
        # (merge happened, no record of what moved). Carry the per-table
        # moved-counts so the repoint is auditable / reversible-by-inspection
        # even though we don't build a full undo tool this phase. Logged
        # INSIDE the savepoint so a rollback also undoes the audit row —
        # we never want an audit entry describing a move that didn't happen.
        audit_dao.log_event(
            session,
            event_type="account.merge_repointed",
            source="system",
            user_id=survivor_id,
            actor_id=None,
            metadata={
                "survivor_id": str(survivor_id),
                "absorbed_id": str(absorbed_id),
                "counts": moved_counts,
            },
        )
        session.flush()


def _merge_anchors_and_delete(
    session: Session,
    survivor: User,
    absorbed: User,
    survivor_id: uuid.UUID,
    absorbed_id: uuid.UUID,
) -> None:
    """Shared tail of merge_users for both modes: fold the email/phone
    anchors and the nowlez extension onto the survivor, then delete the
    (now-childless, in repoint mode) absorbed row. Unchanged logic from the
    original repoint=False-only implementation — extracted verbatim so
    repoint=True can run it inside the same SAVEPOINT as the child-table
    moves."""
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
