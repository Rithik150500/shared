"""Shared identity package — phone-canonical auth for Nowlez × Munshi."""
from . import errors
from .api import (
    confirm_wa_login,
    consume_wa_login,
    decode_access_token,
    link_email_to_phone_account,
    login_with_password,
    refresh_access_token,
    revoke_session,
    set_password,
    start_email_otp,
    start_phone_login,
    start_wa_login,
    start_wa_login_from_bot,
    verify_email_otp_and_login,
    verify_otp_and_login,
)

__version__ = "0.1.0"
__all__ = [
    "start_phone_login",
    "verify_otp_and_login",
    "login_with_password",
    "refresh_access_token",
    "revoke_session",
    "set_password",
    "decode_access_token",
    # Unified-auth bridge (Phase 2)
    "start_wa_login",
    "confirm_wa_login",
    "start_wa_login_from_bot",
    "consume_wa_login",
    "start_email_otp",
    "verify_email_otp_and_login",
    "link_email_to_phone_account",
    "errors",
]
