"""Public front door for the identity package.

Brand surfaces (Munshi webhook, Nowlez backend) import from here. Everything
else (otp/, delivery/, session/, password/) is implementation detail and may
change without breaking consumers.
"""
from __future__ import annotations

import logging
import secrets
import uuid

logger = logging.getLogger(__name__)

from sqlalchemy.orm import Session

from data_access.daos import audit_dao, email_otp_dao, login_request_dao, otp_dao, session_dao, user_dao
from data_access.daos.user_dao import MergeUnsafeError
from .config import settings

from .delivery.email import send_security_email  # noqa: F401
from .delivery.router import deliver_email_otp, deliver_otp
from .errors import (
    AccountLinkStepUpRequired,
    EmailDeliveryFailed,
    InvalidCredentials,
    OtpAlreadyUsed,
    OtpAttemptsExhausted,
    OtpInvalid,
    PasswordNotSet,
    RateLimited,
)
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


def start_wa_login(
    session: Session,
    *,
    brand: str = "nowlez",
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """web2bot start. Returns raw nonce + poll_secret (web wrapper sets the
    poll cookie and builds wa_me_url; identity returns raw values only)."""
    nonce = secrets.token_urlsafe(settings.WA_LOGIN_NONCE_LENGTH)
    poll_secret = secrets.token_urlsafe(settings.WA_LOGIN_NONCE_LENGTH)
    lr = login_request_dao.create_web2bot(
        session,
        token_hash=session_dao._hash(nonce),
        brand=brand,
        poll_bind_hash=session_dao._hash(poll_secret),
        ttl_seconds=settings.WA_LOGIN_NONCE_TTL_WEB2BOT_SECONDS,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    audit_dao.log_event(
        session,
        event_type="wa_login.started",
        source="identity" if brand == "nowlez" else brand,
        metadata={"direction": "web2bot"},
        ip_address=ip_address,
    )
    return {
        "login_id": str(lr.id),
        "nonce": nonce,
        "poll_secret": poll_secret,
        "expires_at": lr.expires_at.isoformat() if lr.expires_at is not None else "",
    }


def confirm_wa_login(session: Session, *, nonce: str, user, brand: str = "munshi") -> bool:
    """Bot login_bridge calls this with the proven (already-committed) sender
    user. Atomic pending->confirmed; branch ONLY on rowcount. Never get_or_create
    (a web-initiated login must not silently provision a Nowlez account)."""
    if user is None:
        # No resolvable user for the proven phone -> diagnostic, fail closed.
        lr = login_request_dao.get_active_by_token(
            session, token_hash=session_dao._hash(nonce), statuses=("pending",)
        )
        if lr is not None:
            login_request_dao.mark_expired(session, lr.id)
        audit_dao.log_event(
            session, event_type="wa_login.confirm_no_user", source=brand,
            metadata={"direction": "web2bot"},
        )
        return False
    rc = login_request_dao.confirm(
        session, token_hash=session_dao._hash(nonce), user_id=user.id, phone=user.phone
    )
    if rc == 1:
        audit_dao.log_event(
            session, event_type="wa_login.confirmed", source=brand, user_id=user.id,
            metadata={"direction": "web2bot"},
        )
        return True
    return False


def start_wa_login_from_bot(
    session: Session,
    *,
    user_id: uuid.UUID,
    brand: str = "munshi",
    ttl: int | None = None,
) -> str:
    """bot2web. Creates a PENDING row (NOT pre-confirmed) and returns the raw
    nonce; the handler flips pending->confirmed ONLY on a successful wamid so a
    live undelivered bearer secret never exists."""
    user = user_dao.get_by_id(session, user_id)
    phone = user.phone if user is not None else None
    nonce = secrets.token_urlsafe(settings.WA_LOGIN_NONCE_LENGTH)
    login_request_dao.create_bot2web(
        session,
        token_hash=session_dao._hash(nonce),
        brand=brand,
        user_id=user_id,
        phone=phone,
        ttl_seconds=ttl or settings.WA_LOGIN_NONCE_TTL_BOT2WEB_SECONDS,
    )
    audit_dao.log_event(
        session, event_type="wa_login.started", source=brand, user_id=user_id,
        metadata={"direction": "bot2web"},
    )
    return nonce


def consume_wa_login(
    session: Session,
    *,
    login_id: uuid.UUID | None = None,
    token: str | None = None,
    poll_bind: str | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> dict | None:
    """Confirmed->consumed mint. Two call shapes:
      - web2bot status-poll: login_id + poll_bind (the poll-cookie value)
      - bot2web landing: token (raw nonce from the #fragment)
    Returns the _mint_login_response dict, or None on zero rows (caller maps to
    expired/link_expired). The DAO commits the consume flip BEFORE we mint
    (mirror claim_message) so a mint failure cannot un-consume the nonce."""
    if login_id is not None:
        row = login_request_dao.get_by_id(session, login_id)
        brand = row.brand if row is not None else "nowlez"
        user_id = login_request_dao.consume_by_id(
            session, login_id=login_id, poll_bind_hash=session_dao._hash(poll_bind or "")
        )
    elif token is not None:
        row = login_request_dao.get_active_by_token(
            session, token_hash=session_dao._hash(token), statuses=("confirmed",)
        )
        brand = row.brand if row is not None else "nowlez"
        user_id = login_request_dao.consume_by_token(
            session, token_hash=session_dao._hash(token)
        )
    else:
        return None

    if user_id is None:
        audit_dao.log_event(
            session, event_type="wa_login.consume_replayed",
            source="identity", ip_address=ip_address,
        )
        return None

    user = user_dao.get_by_id(session, user_id)
    if user is None:
        return None
    minted = _mint_login_response(
        session,
        user=user,
        brand=brand,
        user_agent=user_agent,
        ip_address=ip_address,
        event_type="user.login.wa",
        audit_metadata={"via": "wa_login"},
    )
    audit_dao.log_event(
        session, event_type="wa_login.consumed", source=brand if brand in ("munshi", "nowlez") else "identity",
        user_id=user.id, ip_address=ip_address,
    )
    return minted


def _canonicalize_email(email: str) -> str:
    """Single choke point: strip + lowercase (incl. casefolded domain). Must
    match how users.email is stored so OTP rows and user rows always join."""
    e = email.strip().lower()
    if "@" in e:
        local, _, domain = e.partition("@")
        e = f"{local}@{domain.casefold()}"
    return e


def start_email_otp(session: Session, *, email: str, ip_address: str | None = None) -> dict:
    """Begin email-OTP flow. Anti-enumeration: identical shape regardless of
    registration; returns 200 even on delivery soft-fail (row marked failed)."""
    email = _canonicalize_email(email)
    if len(email) > 254:
        raise ValueError("email too long")

    # Rate limit per email + per IP (mirror check_otp_rate_limit).
    if email_otp_dao.count_within(session, email, 60) >= settings.OTP_PER_EMAIL_PER_HOUR:
        raise RateLimited(retry_after_seconds=3600)
    if ip_address and email_otp_dao.count_by_ip_within(session, ip_address, 60) >= settings.OTP_PER_IP_PER_HOUR:
        raise RateLimited(retry_after_seconds=3600)

    code = generate_otp_code()
    o = email_otp_dao.insert(
        session, email=email, code_hash=hash_otp_code(code),
        ttl_minutes=settings.OTP_TTL_MINUTES, ip_address=ip_address,
    )
    try:
        _channel, provider_id = deliver_email_otp(email, code)
        email_otp_dao.mark_delivered(session, o.id, provider_id=provider_id)
    except EmailDeliveryFailed:
        # Soft-fail: mark failed, still return otp_id (anti-enumeration). The
        # web wrapper bumps otp_delivery_total{channel='email',result='fail'}.
        email_otp_dao.mark_failed(session, o.id)

    audit_dao.log_event(
        session, event_type="email_otp.issued", source="identity",
        metadata={"otp_id": str(o.id), "channel": "email"}, ip_address=ip_address,
    )
    return {"otp_id": str(o.id), "channel": "email"}


def _verify_email_otp_atomic(session: Session, *, otp_id: uuid.UUID, code: str) -> str:
    """Atomic email-OTP verify. argon2 first; mismatch -> decrement (no burn);
    match -> atomic email_otp_dao.mark_used (rowcount==0 -> already-used/expired).
    Returns the canonical email on success."""
    from argon2 import PasswordHasher
    from argon2 import exceptions as a2err
    o = email_otp_dao.get_by_id(session, otp_id)
    if o is None:
        raise OtpInvalid("OTP not found")
    if o.used_at is not None:
        raise OtpAlreadyUsed("OTP has already been used")
    if o.attempts_remaining <= 0:
        raise OtpAttemptsExhausted("OTP attempts exhausted; request a new code")
    hasher = PasswordHasher(
        time_cost=settings.ARGON2_TIME_COST,
        memory_cost=settings.ARGON2_MEMORY_COST_KB,
        parallelism=settings.ARGON2_PARALLELISM,
    )
    try:
        hasher.verify(o.code_hash, code)
    except a2err.VerifyMismatchError:
        email_otp_dao.decrement_attempts(session, otp_id)
        raise OtpInvalid("Invalid OTP code")
    except a2err.InvalidHash:
        raise OtpInvalid("OTP record corrupted")
    if email_otp_dao.mark_used(session, otp_id) == 0:
        raise OtpAlreadyUsed("OTP has already been used")
    return o.email


def verify_email_otp_and_login(
    session: Session,
    *,
    otp_id: uuid.UUID | str,
    code: str,
    brand: str,
    name: str | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
    second_signal_user_id: uuid.UUID | None = None,
) -> dict:
    """Email-OTP verify + §8.1 safeguarded account resolution + mint.

    second_signal_user_id: a user id the SAME flow has already proven control of
    (phone-OTP / one-tap in this browser). When it matches the resolved account
    it satisfies the D4 'second matching signal' gate and the email is linked.
    """
    if isinstance(otp_id, str):
        otp_id = uuid.UUID(otp_id)
    email = _verify_email_otp_atomic(session, otp_id=otp_id, code=code)

    existing = user_dao.get_by_email(session, email)
    if existing is None:
        # Clean new signup: create email-only user, mark verified, mint.
        user, was_created = user_dao.get_or_create_by_email(session, email=email)
        if brand == "nowlez":
            user_dao.ensure_nowlez_extension(session, user.id, name=name or "")
        user_dao.set_email_verified(session, user.id)
        return _mint_login_response(
            session, user=user, brand=brand, name=name,
            user_agent=user_agent, ip_address=ip_address,
            event_type="user.created", audit_metadata={"channel": "email"},
        )

    # Existing account for this email.
    if user_dao.is_email_verified(session, existing.id):
        return _mint_login_response(
            session, user=existing, brand=brand, name=name,
            user_agent=user_agent, ip_address=ip_address,
            event_type="user.login.email", audit_metadata={"channel": "email"},
        )

    # Unverified email on an existing account.
    has_second_factor = existing.phone is not None or existing.password_hash is not None
    if not has_second_factor:
        # Pure email-only legacy row: email control is the only anchor -> mint.
        if brand == "nowlez":
            user_dao.ensure_nowlez_extension(session, existing.id, name=name or "")
        user_dao.set_email_verified(session, existing.id)
        return _mint_login_response(
            session, user=existing, brand=brand, name=name,
            user_agent=user_agent, ip_address=ip_address,
            event_type="user.login.email", audit_metadata={"channel": "email"},
        )

    # Second-signal gate: only link if a same-flow proven identifier matches.
    if second_signal_user_id is not None and second_signal_user_id == existing.id:
        if brand == "nowlez":
            user_dao.ensure_nowlez_extension(session, existing.id, name=name or "")
        user_dao.set_email_verified(session, existing.id)
        audit_dao.log_event(
            session, event_type="account.email_login_new_device", source=brand,
            user_id=existing.id, metadata={"channel": "email"}, ip_address=ip_address,
        )
        _notify_account_security(session, existing, event="email_login_new_device", ip_address=ip_address)
        return _mint_login_response(
            session, user=existing, brand=brand, name=name,
            user_agent=user_agent, ip_address=ip_address,
            event_type="user.login.email", audit_metadata={"channel": "email"},
        )

    audit_dao.log_event(
        session, event_type="email_otp.verify_failed", source=brand,
        user_id=existing.id, metadata={"reason": "step_up_required"}, ip_address=ip_address,
    )
    raise AccountLinkStepUpRequired("verify by phone to link this email")


_SECURITY_COPY = {
    "email_login_new_device": (
        "New sign-in to your Nowlez account",
        "We detected a new email sign-in / email link on your account. "
        "If this wasn't you, reply to this email or contact support to revoke access immediately.",
    ),
    "account_merged": (
        "Your Nowlez accounts were linked",
        "Two of your Nowlez identities (phone and email) were just merged into one account. "
        "If you did not request this, contact support immediately to reverse it.",
    ),
}


def _notify_account_security(
    session: Session,
    user,
    *,
    event: str,
    ip_address: str | None = None,
) -> None:
    """D4 safeguard: best-effort, user-VISIBLE security alert across the account's
    other channels. NEVER raises — a delivery failure must not block login/merge.
    Always audits the outcome so a silently-dead notifier is detectable."""
    subject, body = _SECURITY_COPY.get(
        event, ("Security notice on your Nowlez account", "A sensitive change occurred on your account.")
    )
    delivered = False
    # Channel 1 — email (primary; reliable once Resend is configured).
    if getattr(user, "email", None):
        try:
            send_security_email(user.email, f"[{event}] {subject}", body)
            delivered = True
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.warning("security alert email failed for user %s: %s", user.id, e)
    # Channel 2 — WhatsApp (best-effort; only if a template is configured + user has a phone).
    if getattr(user, "phone", None) and settings.META_SECURITY_TEMPLATE_NAME:
        try:
            from whatsapp_delivery import TemplateClient
            TemplateClient(
                phone_number_id=settings.META_PHONE_NUMBER_ID,
                access_token=settings.META_ACCESS_TOKEN,
            ).send_template_with_components(
                to=user.phone, name=settings.META_SECURITY_TEMPLATE_NAME,
                language="en", body_variables=[event],
            )
            delivered = True
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.warning("security alert WhatsApp failed for user %s: %s", user.id, e)
    audit_dao.log_event(
        session,
        event_type="account.security_alert_sent" if delivered else "account.security_alert_failed",
        source="identity",
        user_id=user.id,
        metadata={"event": event},
        ip_address=ip_address,
    )


def link_email_to_phone_account(
    session: Session,
    *,
    phone_user_id: uuid.UUID,
    email_user_id: uuid.UUID,
) -> bool:
    """D4 auto-merge, invoked ONLY when one flow proved control of BOTH
    identifiers. Survivor = older created_at. Refuses (and audits
    account.merge_conflict) if the two rows own DISTINCT phones — that needs
    human resolution. Returns True on merge, False on refusal/no-op."""
    a = user_dao.get_by_id(session, phone_user_id)
    b = user_dao.get_by_id(session, email_user_id)
    if a is None or b is None or a.id == b.id:
        return False
    if a.phone is not None and b.phone is not None and a.phone != b.phone:
        audit_dao.log_event(
            session, event_type="account.merge_conflict", source="identity",
            user_id=a.id, metadata={"other_user_id": str(b.id)},
        )
        return False
    survivor, absorbed = (a, b) if a.created_at <= b.created_at else (b, a)
    try:
        user_dao.merge_users(session, survivor_id=survivor.id, absorbed_id=absorbed.id)
    except MergeUnsafeError as exc:
        audit_dao.log_event(
            session, event_type="account.merge_conflict", source="identity",
            user_id=survivor.id, metadata={"other_user_id": str(absorbed.id), "reason": str(exc)},
        )
        return False
    audit_dao.log_event(
        session, event_type="account.merged", source="identity", user_id=survivor.id,
        metadata={"survivor": str(survivor.id), "absorbed": str(absorbed.id)},
    )
    _notify_account_security(session, survivor, event="account_merged")
    return True


__all__ = [
    "start_phone_login",
    "verify_otp_and_login",
    "login_with_password",
    "refresh_access_token",
    "revoke_session",
    "set_password",
    "decode_access_token",
    "start_wa_login",
    "confirm_wa_login",
    "start_wa_login_from_bot",
    "consume_wa_login",
    "start_email_otp",
    "verify_email_otp_and_login",
    "link_email_to_phone_account",
]
