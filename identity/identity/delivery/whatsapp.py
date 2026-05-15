"""WhatsApp OTP delivery via Meta Cloud API authentication template.

Uses ``auth_otp_v1`` template (configured via ``settings.META_AUTH_TEMPLATE_NAME``).
The template body has a single ``{{1}}`` variable (the OTP code) and a Copy-code
button that auto-fills with the same value.

Meta's Cloud API requires:
    * E.164 phone numbers WITHOUT the leading ``+`` (we strip it here).
    * A pre-approved template (transactional messages outside the 24-hour
      session window are otherwise rejected).
    * The template must be in the AUTHENTICATION category for OTP use.
"""
from __future__ import annotations

import httpx

from ..config import settings
from ..errors import DeliveryFailed

META_API_VERSION = "v18.0"


def send_otp_whatsapp(
    phone: str,
    code: str,
    *,
    phone_number_id: str | None = None,
    access_token: str | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """Send an OTP via the Meta WhatsApp Cloud API.

    Parameters
    ----------
    phone:
        Destination phone in E.164 format (e.g. ``+919876543210``). The
        leading ``+`` is stripped before forwarding to Meta.
    code:
        The OTP code to fill into the template body and Copy-code button.
    phone_number_id:
        Override for ``settings.META_PHONE_NUMBER_ID``. Tests use this to
        avoid depending on environment.
    access_token:
        Override for ``settings.META_ACCESS_TOKEN``.
    timeout_seconds:
        Override for ``settings.WHATSAPP_SEND_TIMEOUT_SECONDS``.

    Returns
    -------
    The ``wamid`` (Meta message id) of the queued message.

    Raises
    ------
    DeliveryFailed
        On any HTTP error response or transport-level failure.
    """
    pid = phone_number_id or settings.META_PHONE_NUMBER_ID
    tok = access_token or settings.META_ACCESS_TOKEN
    timeout = timeout_seconds or settings.WHATSAPP_SEND_TIMEOUT_SECONDS

    url = f"https://graph.facebook.com/{META_API_VERSION}/{pid}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone.lstrip("+"),
        "type": "template",
        "template": {
            "name": settings.META_AUTH_TEMPLATE_NAME,
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": code}],
                },
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [{"type": "text", "text": code}],
                },
            ],
        },
    }
    headers = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            raise DeliveryFailed("whatsapp", f"HTTP {r.status_code}: {r.text}")
        data = r.json()
        return data["messages"][0]["id"]
    except httpx.HTTPError as e:
        raise DeliveryFailed("whatsapp", str(e))
