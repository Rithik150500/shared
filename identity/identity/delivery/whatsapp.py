"""WhatsApp OTP delivery via the shared ``whatsapp_delivery`` package.

Uses ``auth_otp_v1`` (configured via ``settings.META_AUTH_TEMPLATE_NAME``).
The template body has a single ``{{1}}`` variable (the OTP code) and a
Copy-code URL button that mirrors the same value -- Meta's
AUTHENTICATION-category template shape.

History (audit D-12): this module used to embed its own Meta Cloud API
client (httpx POST + payload construction + error mapping). That created
three parallel implementations of "send a WhatsApp template" across the
codebase and meant the OTP path missed every centralised fix landed in
``whatsapp_delivery``:

    * B-3 worker-side dedup (idempotency by wamid)
    * D-4 producer-side ``dedup_key`` short-circuit
    * D-6 template variable sanitization (control chars, RTL overrides,
      ``{{N}}`` placeholder defusing, length cap)
    * D-9 ``META_TEMPLATES_FALLBACK_TO_TEXT`` observability
    * D-11 single source of truth for the Graph API version (v20.0)

After D-12 this module is a thin adapter: it constructs a
``TemplateClient`` per call (the client is just a dataclass with two
strings -- no connection state to amortise), invokes
``send_template_with_components`` with the OTP code as both the body
variable and the URL-button variable, and translates the typed
``whatsapp_delivery`` exceptions into ``DeliveryFailed`` so the router's
WhatsApp -> SMS fallback continues to work.

Why a synchronous direct call instead of the queue?
    OTPs are LATENCY-CRITICAL: the user is blocked in a login flow. The
    queue (``enqueue_send_template_with_components``) targets background
    fan-out where ~minutes of latency is acceptable. The synchronous
    ``TemplateClient.send_template_with_components`` is the right primitive
    here -- it POSTs and returns the wamid in-line.
"""
from __future__ import annotations

import httpx
from whatsapp_delivery import (
    Meta24HourWindowExpired,
    MetaInvalidMessage,
    MetaTransientError,
    TemplateClient,
)

from ..config import settings
from ..errors import DeliveryFailed


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
        leading ``+`` is stripped before forwarding to Meta (inside
        ``TemplateClient``).
    code:
        The OTP code to fill into the template body and Copy-code button.
    phone_number_id:
        Override for ``settings.META_PHONE_NUMBER_ID``. Tests use this to
        avoid depending on environment.
    access_token:
        Override for ``settings.META_ACCESS_TOKEN``.
    timeout_seconds:
        Override for ``settings.WHATSAPP_SEND_TIMEOUT_SECONDS``. Threaded
        into ``TemplateClient.send_template_with_components`` so OTP sends
        stay bounded by identity's tighter latency budget (the
        ``whatsapp_delivery`` module default is 30s, which is too long
        for a user blocked in a login flow).

    Returns
    -------
    The ``wamid`` (Meta message id) of the queued message.

    Raises
    ------
    DeliveryFailed
        On any HTTP error response or transport-level failure. All typed
        exceptions from ``whatsapp_delivery`` (``MetaTransientError``,
        ``MetaInvalidMessage``, ``Meta24HourWindowExpired``) and raw httpx
        transport errors collapse into this single exception so the
        delivery router's WhatsApp -> SMS fallback is unaffected by the
        D-12 refactor.
    """
    pid = phone_number_id or settings.META_PHONE_NUMBER_ID
    tok = access_token or settings.META_ACCESS_TOKEN
    timeout = timeout_seconds or settings.WHATSAPP_SEND_TIMEOUT_SECONDS

    client = TemplateClient(phone_number_id=pid, access_token=tok)
    try:
        return client.send_template_with_components(
            to=phone,
            name=settings.META_AUTH_TEMPLATE_NAME,
            language="en",
            # The auth template's single body variable is the OTP code.
            body_variables=[code],
            # The Copy-code button auto-fills with the same code -- Meta's
            # AUTHENTICATION-category template requires both slots to carry
            # the OTP value (one for the message body, one for the button).
            button_url_variables=[code],
            timeout_seconds=timeout,
        )
    except (MetaTransientError, MetaInvalidMessage, Meta24HourWindowExpired) as e:
        # Collapse the typed whatsapp_delivery exceptions into the identity
        # package's single delivery-failure type so callers (notably the
        # delivery.router fallback path) continue to see a uniform interface.
        raise DeliveryFailed("whatsapp", str(e)) from e
    except httpx.HTTPError as e:
        # Network-level failures (connect timeout, DNS failure, etc.) skip
        # the response-classification path inside whatsapp_delivery and
        # bubble out as raw httpx errors; map them to DeliveryFailed here
        # so the router can fall back to SMS.
        raise DeliveryFailed("whatsapp", str(e)) from e
