"""Public front door for the identity package.

Brand surfaces (Munshi webhook, Nowlez backend) import from here. Everything
else (otp/, delivery/, session/, password/) is implementation detail and may
change without breaking consumers.
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from data_access.daos import audit_dao, otp_dao, session_dao, user_dao

from .delivery.router import deliver_otp
from .errors import InvalidCredentials, PasswordNotSet
from .otp.issuer import generate_otp_code, hash_otp_code
from .otp.rate_limiter import check_otp_rate_limit
from .otp.verifier import verify_otp as _verify_otp, verify_otp_atomic as _verify_otp_atomic
from .password.hasher import BOGUS_HASH, hash_password, needs_rehash, verify_password
from .password.validator import validate_password_strength
from .session.jwt import decode_access_token, encode_access_token
from .session.refresh import (
    consume_refresh_token,
    issue_refresh_token,
    revoke_refresh_token,
)


def start_phone_login(
    session: Session,
    *,
    phone: str,
    ip_address: str | None = None,
) -> dict:
    """Begin phone-OTP flow: rate-limit check -> mint code -> store hash -> deliver.

    Returns {"otp_id": str, "channel": "whatsapp" | "sms"}.

    Anti-enumeration: the response shape is identical regardless of whether the
    phone is registered. User creation happens at verify time.
    """
    check_otp_rate_limit(session, phone=phone, ip_address=ip_address)
    code = generate_otp_code()
    o = otp_dao.insert(
        session,
        phone=phone,
        code_hash=hash_otp_code(code),
        channel="whatsapp",  # router may downgrade to sms
        ttl_minutes=10,
        ip_address=ip_address,
    )
    try:
        channel, provider_id = deliver_otp(phone, code)
        otp_dao.mark_delivered(session, o.id, provider_id=provider_id)
        if channel != o.channel:
            # Router fell back; reflect actual channel on the row
            o.channel = channel
            session.flush()
    except Exception:
        otp_dao.mark_failed(session, o.id)
        raise

    audit_dao.log_event(
        session,
        event_type="otp.issued",
        source="identity",
        metadata={"otp_id": str(o.id), "channel": channel},
        ip_address=ip_address,
    )
    return {"otp_id": str(o.id), "channel": channel}


def _mint_login_response(
    session: Session,
    *,
    user,
    brand: str,
    name: str | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
    was_created: bool = False,
    event_type: str | None = None,
    audit_metadata: dict | None = None,
) -> dict:
    """Single session-mint path shared by every login method (spec §3.2).

    Ensures the brand extension, touches last_login, issues refresh + access,
    writes an audit event, and returns the invariant token dict. brand in
    {'munshi','nowlez'}.
    """
    if brand == "munshi":
        user_dao.ensure_munshi_extension(session, user.id)
    elif brand == "nowlez":
        user_dao.ensure_nowlez_extension(session, user.id, name=name or "")

    user_dao.touch_last_login(session, user.id)
    refresh_raw, _ = issue_refresh_token(
        session, user_id=user.id, user_agent=user_agent, ip_address=ip_address
    )
    access = encode_access_token(user.id)

    if event_type is None:
        event_type = "user.created" if was_created else "user.login.otp"
    audit_dao.log_event(
        session,
        event_type=event_type,
        user_id=user.id,
        source=brand,
        metadata=audit_metadata or {},
        ip_address=ip_address,
    )
    return {
        "access_token": access,
        "refresh_token": refresh_raw,
        "user": {"id": str(user.id), "phone": user.phone, "locale": user.locale},
    }


def verify_otp_and_login(
    session: Session,
    *,
    otp_id: uuid.UUID | str,
    code: str,
    brand: str,
    name: str | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> dict:
    """Verify the submitted OTP, find-or-create the user, issue tokens.

    brand: "munshi" or "nowlez" -- controls which 1:1 extension is attached.
    For "nowlez", `name` is required (used to populate users_nowlez.name).

    Returns {"access_token", "refresh_token", "user": {"id", "phone", "locale"}}.
    """
    if isinstance(otp_id, str):
        otp_id = uuid.UUID(otp_id)
    o = otp_dao.get_by_id(session, otp_id)
    _verify_otp_atomic(session, otp_id=otp_id, code=code)

    user, was_created = user_dao.get_or_create_by_phone(session, phone=o.phone)
    return _mint_login_response(
        session,
        user=user,
        brand=brand,
        name=name,
        user_agent=user_agent,
        ip_address=ip_address,
        was_created=was_created,
        audit_metadata={"channel": o.channel},
    )


def login_with_password(
    session: Session,
    *,
    phone: str,
    password: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> dict:
    """Password-based login. Timing-safe against phone-enumeration."""
    user = user_dao.get_by_phone(session, phone)
    if user is None:
        # Run argon2id verify anyway so unknown-phone takes same time as wrong-pwd
        verify_password("__decoy__", BOGUS_HASH)
        raise InvalidCredentials("phone or password is incorrect")
    if user.password_hash is None:
        verify_password("__decoy__", BOGUS_HASH)
        raise PasswordNotSet("password is not set for this account; use OTP login")
    if not verify_password(password, user.password_hash):
        raise InvalidCredentials("phone or password is incorrect")
    if needs_rehash(user.password_hash):
        user_dao.update_password(session, user.id, hash_password(password))

    user_dao.touch_last_login(session, user.id)
    refresh_raw, _ = issue_refresh_token(
        session, user_id=user.id, user_agent=user_agent, ip_address=ip_address
    )
    access = encode_access_token(user.id)

    audit_dao.log_event(
        session,
        event_type="user.login.password",
        user_id=user.id,
        source="identity",
        ip_address=ip_address,
    )
    return {
        "access_token": access,
        "refresh_token": refresh_raw,
        "user": {"id": str(user.id), "phone": user.phone, "locale": user.locale},
    }


def refresh_access_token(session: Session, *, refresh_token: str) -> dict:
    """Mint a fresh access JWT given a valid refresh token. Raises InvalidToken if dead."""
    s = consume_refresh_token(session, refresh_token)
    return {"access_token": encode_access_token(s.user_id)}


def revoke_session(session: Session, *, refresh_token: str) -> None:
    """Logout -- invalidate this refresh token. Idempotent."""
    revoke_refresh_token(session, refresh_token)


def set_password(
    session: Session,
    *,
    user_id: uuid.UUID,
    new_password: str,
    fresh_otp_id: uuid.UUID | str,
    fresh_otp_code: str,
    current_session_id: uuid.UUID | None = None,
) -> None:
    """Set or change a user's password. Requires a fresh OTP for confirmation.

    On success, optionally revokes all other sessions for this user (when
    current_session_id is provided -- keeps the caller's session alive).
    """
    if isinstance(fresh_otp_id, str):
        fresh_otp_id = uuid.UUID(fresh_otp_id)
    validate_password_strength(new_password)
    _verify_otp(session, otp_id=fresh_otp_id, code=fresh_otp_code)
    h = hash_password(new_password)
    user_dao.update_password(session, user_id, h)
    if current_session_id is not None:
        session_dao.revoke_all_except(session, user_id, except_session_id=current_session_id)
    audit_dao.log_event(
        session, event_type="password.set", user_id=user_id, source="identity"
    )


__all__ = [
    "start_phone_login",
    "verify_otp_and_login",
    "login_with_password",
    "refresh_access_token",
    "revoke_session",
    "set_password",
    "decode_access_token",
]
