"""Shared identity package — phone-canonical auth for Nowlez × Munshi."""
from . import errors
from .api import (
    decode_access_token,
    login_with_password,
    refresh_access_token,
    revoke_session,
    set_password,
    start_phone_login,
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
    "errors",
]
