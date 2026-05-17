"""HMAC-SHA256 webhook verification + parsing.

Razorpay signs every webhook with HMAC-SHA256(secret, raw_body) and sends
the hex digest in the ``X-Razorpay-Signature`` header. Verification MUST
use :func:`hmac.compare_digest` so the comparison runs in constant time
relative to its inputs — protecting against timing-side-channel attacks
that recover the signature byte-by-byte.

Parsing is intentionally lenient: we never raise on missing fields because
the downstream webhook router classifies on ``event`` and dispatches
through the queue, where structural errors become dead-letter rows rather
than 500s back to Razorpay (which would trigger their own retry storm).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WebhookEvent:
    """The slice of a Razorpay webhook payload we care about."""

    id: str
    event: str
    entity: str
    payload: dict[str, Any]
    created_at: int | None


def verify_webhook_signature(
    raw_body: bytes,
    signature: str | None,
    secret: str,
) -> bool:
    """Return True iff ``signature`` is the valid HMAC-SHA256 of ``raw_body``.

    Args:
        raw_body: The exact byte-string Razorpay POSTed; callers MUST pass
            the unparsed body (re-serialized JSON will not match because
            whitespace and key order differ).
        signature: Hex digest from the ``X-Razorpay-Signature`` header.
            Returns False (never raises) for ``None``, empty, or
            malformed hex so the upstream handler can branch on result.
        secret: Webhook secret configured in the Razorpay dashboard.

    Returns:
        True iff the signature matches; False on any failure.
    """
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # compare_digest tolerates same-length inequal strings without raising;
    # different lengths still return False but in constant time relative
    # to the length of the shorter string.
    return hmac.compare_digest(expected, signature)


def parse_webhook_event(payload: dict[str, Any]) -> WebhookEvent:
    """Map a Razorpay webhook JSON body to our :class:`WebhookEvent`.

    Missing fields land as empty string / empty dict / None so callers can
    rely on the attribute shape regardless of payload variations.
    """
    return WebhookEvent(
        id=payload.get("id", "") or "",
        event=payload.get("event", "") or "",
        entity=payload.get("entity", "") or "",
        payload=dict(payload.get("payload") or {}),
        created_at=payload.get("created_at"),
    )
