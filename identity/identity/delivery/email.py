"""Email OTP delivery via Resend (synchronous httpx POST).

Lives in the shared identity package (so the bot can reuse it). Deliberately
synchronous — never bridge to casepilot's async backend/email.py from a sync
caller (asyncio deadlock risk). Mirrors sms.py: typed override args for tests,
collapse all failures into EmailDeliveryFailed so start_email_otp can soft-fail.
"""
from __future__ import annotations

import httpx

from ..config import settings
from ..errors import EmailDeliveryFailed


def send_otp_email(
    email: str,
    code: str,
    *,
    api_key: str | None = None,
    from_addr: str | None = None,
    timeout_seconds: float | None = None,
) -> tuple[str, str]:
    """Send the OTP code via Resend. Returns (channel='email', provider_id).

    Raises EmailDeliveryFailed on non-2xx or transport error.
    """
    key = api_key or settings.RESEND_API_KEY
    sender = from_addr or settings.EMAIL_OTP_FROM
    timeout = timeout_seconds or settings.EMAIL_SEND_TIMEOUT_SECONDS

    payload = {
        "from": sender,
        "to": [email],
        "subject": "Your Nowlez login code",
        "text": f"Your verification code is {code}. It expires in 10 minutes.",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
        if r.status_code // 100 != 2:
            raise EmailDeliveryFailed(f"Resend {r.status_code}: {r.text}")
        try:
            provider_id = r.json().get("id", "")
        except ValueError:
            provider_id = ""
        return ("email", provider_id)
    except httpx.HTTPError as e:
        raise EmailDeliveryFailed(str(e)) from e


def send_security_email(
    to_email: str,
    subject: str,
    body: str,
    *,
    api_key: str | None = None,
    from_addr: str | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """Best-effort transactional security alert via Resend. Returns provider id.
    Raises EmailDeliveryFailed on non-2xx / transport error (caller swallows)."""
    key = api_key or settings.RESEND_API_KEY
    frm = from_addr or settings.EMAIL_OTP_FROM
    timeout = timeout_seconds or settings.EMAIL_SEND_TIMEOUT_SECONDS
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"from": frm, "to": [to_email], "subject": subject, "text": body},
            )
    except httpx.HTTPError as e:
        raise EmailDeliveryFailed(str(e)) from e
    if resp.status_code // 100 != 2:
        raise EmailDeliveryFailed(f"{resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json().get("id", "")
    except ValueError:
        return ""
