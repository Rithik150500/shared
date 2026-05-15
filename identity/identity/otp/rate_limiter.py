"""Rate-limit checks before issuing a fresh OTP.

Queries `otp_codes` for recent issuances; no separate rate-limit table required.
Phone limit blocks per-victim abuse (5/hour); IP limit blocks per-attacker abuse
(20/hour across different phones).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from data_access.daos import otp_dao

from ..config import settings
from ..errors import RateLimited


def check_otp_rate_limit(session: Session, *, phone: str, ip_address: str | None) -> None:
    """Raise RateLimited if per-phone or per-IP OTP-issue budget is exceeded."""
    phone_count = otp_dao.count_within(session, phone=phone, minutes=60)
    if phone_count >= settings.OTP_PER_PHONE_PER_HOUR:
        raise RateLimited(retry_after_seconds=3600)

    if ip_address:
        ip_count = otp_dao.count_by_ip_within(session, ip_address=ip_address, minutes=60)
        if ip_count >= settings.OTP_PER_IP_PER_HOUR:
            raise RateLimited(retry_after_seconds=3600)
