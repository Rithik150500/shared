"""SMS OTP delivery via MSG91 DLT-templated API.

Uses MSG91's ``/api/v5/otp`` endpoint with a pre-registered DLT template.
Sender ID + template are configured via ``settings.MSG91_*``.

MSG91 quirks worth noting:
    * The endpoint accepts **query string parameters** (not a JSON body).
    * The destination ``mobile`` must omit the leading ``+`` from E.164.
    * A 200 OK with ``{"type": "error"}`` is still a failure — we must
      check the JSON ``type`` field, not just the HTTP status code.
    * The DLT template id must be pre-approved with MSG91 (see Task 3).
"""
from __future__ import annotations

import httpx

from ..config import settings
from ..errors import DeliveryFailed


def send_otp_sms(
    phone: str,
    code: str,
    *,
    auth_key: str | None = None,
    template_id: str | None = None,
    sender_id: str | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """Send an OTP via MSG91's DLT-templated SMS endpoint.

    Parameters
    ----------
    phone:
        Destination phone in E.164 format (e.g. ``+919876543210``). The
        leading ``+`` is stripped before forwarding to MSG91.
    code:
        The OTP code to substitute into the DLT template.
    auth_key:
        Override for ``settings.MSG91_AUTH_KEY``. Tests use this to avoid
        depending on environment.
    template_id:
        Override for ``settings.MSG91_OTP_TEMPLATE_ID``.
    sender_id:
        Override for ``settings.MSG91_SENDER_ID``.
    timeout_seconds:
        Override for ``settings.SMS_SEND_TIMEOUT_SECONDS``.

    Returns
    -------
    The MSG91 ``request_id`` of the queued message.

    Raises
    ------
    DeliveryFailed
        On HTTP error status, ``type != "success"`` payloads, or any
        transport-level failure.
    """
    key = auth_key or settings.MSG91_AUTH_KEY
    tpl = template_id or settings.MSG91_OTP_TEMPLATE_ID
    sender = sender_id or settings.MSG91_SENDER_ID
    timeout = timeout_seconds or settings.SMS_SEND_TIMEOUT_SECONDS

    url = "https://api.msg91.com/api/v5/otp"
    params = {
        "template_id": tpl,
        "mobile": phone.lstrip("+"),
        "authkey": key,
        "otp": code,
        "sender": sender,
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, params=params)
        # MSG91 responds with JSON; type=success on accepted message.
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or data.get("type") != "success":
            raise DeliveryFailed("sms", f"MSG91 {r.status_code}: {data}")
        return data.get("request_id", "")
    except httpx.HTTPError as e:
        raise DeliveryFailed("sms", str(e))
